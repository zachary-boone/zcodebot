# CodeBot

CodeBot 是一个协议中立、可扩展的 AI Coding Agent。它以**桌面版（Electron + React）**为主要入口，同时保留**终端 TUI**，两者共用同一套 Python 引擎，用自然语言即可让 Agent 自主完成读代码、改文件、跑命令、验证结果等完整编程任务。

引擎核心是一套约 2 万行的 Python 包：ReAct / Plan 双模式推理循环、Anthropic / OpenAI / OpenAI 兼容三协议客户端、六层权限管道、上下文压缩、跨会话记忆、代码 RAG，以及 MCP / Skill / Hook / 子 Agent 扩展体系。桌面端只是事件流消费者，通过 FastAPI + WebSocket 桥接同一份 `agent.run()` 事件流，因此双端行为一致，无需为桌面端维护第二套 Agent 逻辑。

## 核心特性

- **双端共用同一引擎** — Textual TUI 与 Electron + React 桌面端共用 `codebot.agent`；桌面端由 FastAPI sidecar（`codebot/server.py`，`127.0.0.1:7800`）桥接 WebSocket 事件流
- **ReAct + Plan 双模式** — 默认 ReAct 推理-行动循环；复杂、高风险任务可先切 Plan 模式生成 Markdown 计划，确认后再执行
- **协议中立** — 统一 `LLMClient.stream()` 接口，适配 Anthropic、OpenAI Responses、OpenAI Chat Completions（兼容 DeepSeek、Qwen、Ollama、vLLM 等），切换模型只需改配置
- **代码 RAG（CodeSearch）** — AST 按函数/类分块 + 嵌入式 Qdrant + 自实现 BM25 + RRF 混合检索，mtime + md5 增量索引；未安装向量库时自动降级，不阻塞主流程
- **22 个内置工具 + 延迟加载** — 文件读写、Bash、搜索、子 Agent、团队、任务管理、AskUser、结构化输出等；低频工具通过 ToolSearch 按需发现
- **六层权限管道** — 只读白名单 → 危险命令黑名单 → 路径沙箱 → 规则引擎 → 模式矩阵 → 人工确认，支持 6 种权限模式
- **五大扩展机制** — Tool / Skill / MCP / Hook / 子 Agent 互相独立：4 个内置 Skill、15 种 Hook 生命周期事件、MCP Server 自动桥接
- **上下文管理** — 工具大输出落盘换占位符、LLM 九段结构化摘要、RecoveryState 快照、压缩失败断路器
- **跨会话记忆** — 自动提取用户偏好、反馈、项目知识、参考资料，embedding 语义召回优先、LLM 选择器兜底
- **工程化细节** — Git worktree 任务隔离、文件历史快照回退、多会话切换、运行时切换工作目录、启动配置校验

## 架构设计

```text
┌───────────────────────────────────────────────────────┐
│ 交互层  Textual TUI  |  Electron + React 桌面端        │
│  (进程内事件流消费)   |  (FastAPI + WebSocket 事件流)    │
├───────────────────────────────────────────────────────┤
│ 引擎层  agent.py  ReAct Loop / Plan Mode / Compact     │
│  async generator 逐条 yield AgentEvent                 │
├───────────────────────────────────────────────────────┤
│ 工具层  22 个内置工具 + ToolSearch + MCP + Skill        │
├───────────────────────────────────────────────────────┤
│ 记忆层  Session / Auto Memory / Semantic Recall        │
├───────────────────────────────────────────────────────┤
│ 安全层  白名单 / 黑名单 / 沙箱 / 规则 / 模式 / 人工确认   │
└───────────────────────────────────────────────────────┘
```

核心链路：`Agent.run()` 是 async generator，把新 token、思考过程、工具调用与结果、权限请求、压缩通知等逐条 `yield` 出来。TUI 在进程内消费这些事件；桌面端由 `server.py` 序列化成 JSON 经 `/ws/chat` 转发，前端回传的权限响应再通过 `asyncio.Future.set_result()` 解除 Agent 挂起。两套界面共享同一份引擎，没有为桌面端另写 Agent。

### ReAct 主循环

1. **Think** — LLM 推理当前上下文，决定下一步行动
2. **Act** — 调用工具（读文件、写代码、跑命令、搜索等）
3. **Observe** — 收集工具执行结果
4. **Loop** — 观察结果回灌 LLM 继续推理，直到任务完成或达到最大轮次

期间自动触发：权限检查 → Hook 回调 → 上下文压缩 → 记忆提取。同一轮内只读工具按 `is_concurrency_safe` 分批 `asyncio.gather` 并行执行，写操作保持串行。

### Plan Mode

开启后 Agent 先探索代码并生成 Markdown 计划，经用户确认后才执行写操作，适合重构、迁移等高风险任务。

## 快速开始

### 环境要求

- Python >= 3.11
- Node.js >= 18（仅桌面端需要）
- 推荐 [uv](https://docs.astral.sh/uv/) 管理依赖
- 任一模型 API Key：Anthropic、OpenAI，或任何 OpenAI 兼容服务

### 安装

```bash
git clone https://github.com/zachary-boone/zcodebot.git && cd zcodebot

# 创建虚拟环境并安装（TUI 基础版）
uv venv
uv pip install -e .

# 桌面端 + RAG（桌面端需要 desktop extra，RAG 可选）
uv pip install -e ".[desktop,rag]"
```

不装 `rag` 时语义检索自动降级为 BM25 / Grep；不装 `desktop` 时只使用 TUI 和 CLI。

### 安装桌面端

```bash
cd codebot-desktop
npm install
```

Windows 下也可以直接运行 `启动桌面端.bat`（快捷方式启动用 `启动桌面端.vbs`），脚本会自动检查 7800 端口、提示缺失依赖，并把日志写入 `codebot-desktop/codebot.log`。

### 配置 API Key

```powershell
$env:ANTHROPIC_API_KEY="your-key"   # Windows PowerShell
set ANTHROPIC_API_KEY=your-key       # Windows CMD
export ANTHROPIC_API_KEY=your-key    # Linux/macOS

$env:OPENAI_API_KEY="your-key"
```

### 创建配置文件

配置按 `~/.codebot/config.yaml` → `<project>/.codebot/config.yaml` → `<project>/.codebot/config.local.yaml` 顺序加载合并，后者覆盖前者。最小示例：

```yaml
providers:
  - name: deepseek
    protocol: openai-compat
    base_url: https://api.deepseek.com
    model: deepseek-chat
    api_key: ${OPENAI_API_KEY}
    thinking: false

permission_mode: default

mcp_servers: []

enable_fork: false
enable_verification_agent: false
teammate_mode: ""
enable_coordinator_mode: false

hooks: []

worktree:
  symlink_directories:
    - node_modules
    - .venv
  stale_cleanup_interval: 3600
  stale_cutoff_hours: 24
```

### 启动

```bash
# 桌面版（开发模式，Electron 会自动拉起 FastAPI sidecar）
cd codebot-desktop
npm run dev:electron

# 终端 TUI
codebot

# 命令行单次执行
codebot -p "帮我写一个 Python 快速排序函数"

# 只启动桥接服务（桌面端前端已构建时使用）
codebot-server --host 127.0.0.1 --port 7800
```

常用 CLI 参数：

| 参数 | 说明 |
|------|------|
| `-p PROMPT` | 非交互执行提示词并输出结果到 stdout |
| `--mode <mode>` | 覆盖权限模式：`default` / `acceptEdits` / `plan` / `bypassPermissions` / `custom` / `dontAsk` |
| `--web` | textual serve 模式，使用纯净平台驱动 |

## 配置详解

### providers

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | 是 | 自定义名称 |
| `protocol` | 是 | `anthropic` / `openai` / `openai-compat` |
| `base_url` | 是 | API 地址，OpenAI 兼容服务填对应 `/v1` 地址 |
| `model` | 是 | 模型名 |
| `api_key` | 否 | 密钥或 `${ENV_VAR}` 引用；留空时按协议读取 `ANTHROPIC_API_KEY` / `OPENAI_API_KEY` |
| `thinking` | 否 | 是否启用思考模式，默认 `false` |
| `context_window` | 否 | 显式覆盖上下文窗口；不填时按自动拉取 / 内置映射 / 默认值四层回退 |
| `max_output_tokens` | 否 | 覆盖最大输出 token，默认思考模式 64000、普通 8192 |

### 其他配置

- `mcp_servers` — MCP Server 列表（stdio 或 SSE），见下文
- `hooks` — Hook 定义列表，见下文
- `worktree` — symlink 目录与空闲清理参数
- `enable_fork` / `enable_verification_agent` — 子 Agent 与验证 Agent 开关
- `teammate_mode` / `enable_coordinator_mode` — 团队模式与协调者模式开关

## 权限模式

| 模式 | 读取 | 写入 | 命令 | 说明 |
|------|------|------|------|------|
| `default` | 允许 | 询问 | 询问 | 默认安全模式 |
| `acceptEdits` | 允许 | 允许 | 询问 | 自动接受文件编辑 |
| `plan` | 允许 | 询问 | 询问 | 规划模式，写操作先确认 |
| `bypassPermissions` | 允许 | 允许 | 允许 | 跳过权限检查 |
| `custom` | 询问 | 询问 | 询问 | 全量人工确认 |
| `dontAsk` | 允许 | 允许 | 允许 | 不询问直接放行 |

TUI 中按 `Shift+Tab` 可快速切换权限模式。

## 内置工具（22 个）

| 工具 | 功能 |
|------|------|
| `ReadFile` / `WriteFile` / `EditFile` | 读取、创建/覆写、精确 search/replace 编辑文件 |
| `Bash` | 执行 Shell 命令 |
| `Glob` / `Grep` | 文件名模式匹配 / 正则内容搜索 |
| `CodeSearch` | 代码 RAG 语义检索（AST 分块 + 向量/BM25 混合召回） |
| `Agent` | 派发子 Agent（Fork / SubAgent） |
| `TeamCreate` / `TeamDelete` | 创建/删除 Agent 团队 |
| `TaskCreate` / `TaskGet` / `TaskList` / `TaskUpdate` | 后台任务管理 |
| `LoadSkill` | 激活 Skill 技能包 |
| `ToolSearch` | 延迟加载/发现低频工具 |
| `AskUserQuestion` | 向用户提问 |
| `EnterWorktree` / `ExitWorktree` | 进入/退出 Git worktree 隔离 |
| `ExitPlanMode` | 提交计划并退出规划模式 |
| `SyntheticOutput` | 协调者模式结构化输出 |
| `SendMessage` | 团队内消息通信 |

## 斜杠命令

| 命令 | 功能 |
|------|------|
| `/help` | 帮助（别名 `/h`、`/?`） |
| `/plan` | 切换到 Plan 模式 |
| `/compact` | 手动触发上下文压缩 |
| `/clear` | 清除对话历史 |
| `/session` | 会话管理（多会话切换） |
| `/mcp` | 查看 MCP Server 状态 |
| `/memory` | 记忆管理 |
| `/permission` | 权限管理 |
| `/rewind` | 回退到文件历史检查点 |
| `/status` | 显示状态信息 |
| `/skill` | 管理 Skill 技能包 |
| `/tasks` | 查看/取消后台任务 |
| `/trace` | 查看 Agent 父子追踪树 |
| `/worktree` | 管理 Git Worktree |

## Skill 技能包

| 技能 | 说明 |
|------|------|
| `commit` | 分析 git diff 并生成规范 commit |
| `review` | 多维度代码审查（逻辑/安全/性能/风格/可维护性） |
| `test` | 自动生成测试用例 |
| `backend-interview` | 后端面试知识问答 |

内置 Skill 位于 `codebot/skills/builtins/`；用户自定义 Skill 放在 `~/.codebot/skills/` 和 `<project>/.codebot/skills/`，支持 `SKILL.md` 格式与自定义工具。

## 子 Agent 与团队协作

| Agent | 说明 |
|-------|------|
| `general-purpose` | 通用子 Agent，拥有全部工具 |
| `explore` | 代码探索 Agent，专注搜索和理解代码 |
| `plan` | 规划 Agent，专注制定执行计划 |
| `verification` | 验证 Agent，专注审查和验证代码 |

自定义 Agent 定义放在 `~/.codebot/agents/` 和 `<project>/.codebot/agents/`。团队模式下支持共享任务列表、mailbox 消息通信；协调者模式通过 `SyntheticOutput` 聚合多个 Agent 的结论。

## MCP 工具扩展

```yaml
mcp_servers:
  - name: filesystem
    command: npx
    args:
      - -y
      - @modelcontextprotocol/server-filesystem
      - /path/to/allowed/dir
  - name: github
    command: npx
    args:
      - -y
      - @modelcontextprotocol/server-github
    env:
      GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_TOKEN}
```

支持 stdio（`command`）和 SSE（`url`）两种模式，MCP 工具经 `MCPToolWrapper` 桥接进统一 Tool 调度。

## Hook 钩子

```yaml
hooks:
  - id: lint-on-write
    event: post_tool_use
    tool_name: WriteFile
    command: ruff check $FILE_PATH
```

共 15 种生命周期事件：`session_start` / `session_end`、`turn_start` / `turn_end`、`pre_tool_use` / `post_tool_use`、`pre_send` / `post_receive`、`startup` / `shutdown` / `error` / `compact` / `permission_request` / `file_change` / `command_execute`，可在事件前后注入自定义行为。

## 上下文与记忆

- **上下文压缩**：单条工具输出超过 5 万字符落盘换成 `<persisted-output>` 占位符；历史对话由 LLM 压缩成九段结构化摘要；压缩前用 RecoveryState 快照最近文件内容和 Skill 调用，压缩后重新附加；连续失败 3 次触发断路器。
- **跨会话记忆**：每 5 轮后台提取用户偏好、反馈、项目知识、参考资料四类记忆，写入 `memories.md`；召回优先 embedding 语义索引，未命中再回退 LLM 选择器。
- **会话持久化**：JSONL 落盘到 `.codebot/sessions/`，支持从 `compact_boundary` 重建压缩后的状态，重启后继续对话。

## TUI 快捷键

| 快捷键 | 功能 |
|--------|------|
| `Enter` | 发送消息 |
| `Shift+Enter` / `Ctrl+J` | 换行 |
| `Tab` | 命令/文件补全 |
| `Shift+Tab` | 切换权限模式 |
| `Ctrl+O` | 展开/折叠工具调用详情 |
| `Ctrl+C` | 退出 |
| `Escape` | 取消当前操作 |
| `Up` / `Down` | 在补全候选中导航 |
| `@` | 引用文件（自动补全路径） |

## 项目结构

```text
zcodebot/
├── codebot/                  # Python 引擎
│   ├── agent.py              # Agent 主循环（ReAct / Plan，async generator 事件流）
│   ├── app.py                # Textual TUI 应用
│   ├── server.py             # FastAPI + WebSocket 桥接服务（桌面端 sidecar）
│   ├── client.py             # LLM 客户端（Anthropic / OpenAI / OpenAI Compat）
│   ├── config.py             # 配置加载与合并
│   ├── validator.py          # 启动配置校验
│   ├── runtime.py            # CLI 与桌面端共用的引擎初始化
│   ├── commands/             # 斜杠命令系统
│   ├── context/              # 上下文窗口管理（compact / RecoveryState）
│   ├── hooks/                # Hook 钩子系统（15 种事件）
│   ├── mcp/                  # MCP 协议支持
│   ├── memory/               # 会话与跨会话记忆
│   ├── permissions/          # 六层权限管道
│   ├── rag/                  # 代码 RAG（AST 分块 / BM25 / Qdrant / RRF）
│   ├── skills/               # Skill 技能包系统
│   ├── agents/               # 子 Agent 系统
│   ├── teams/                # 多 Agent 团队协作
│   ├── tools/                # 22 个内置工具
│   ├── worktree/             # Git worktree 隔离
│   └── filehistory/          # 文件历史快照
├── codebot-desktop/          # Electron + React + TypeScript 桌面端
│   ├── electron/             # Electron 主进程（拉起 sidecar / 原生目录选择器）
│   └── src/                  # React 前端（聊天 / 文件树 / 设置 / 权限弹窗）
├── tests/                    # pytest 测试（23 个文件，587 个用例）
├── pyproject.toml            # 项目配置与可选依赖
├── uv.lock
└── README.md
```

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python >= 3.11 |
| TUI | Textual >= 2.1.0 |
| LLM SDK | Anthropic >= 0.42.0 / OpenAI >= 1.60.0 |
| 数据验证 | Pydantic >= 2.0 |
| MCP | mcp >= 1.12.0 |
| 配置解析 | PyYAML >= 6.0 |
| 桌面桥接 | FastAPI >= 0.115.0 + Uvicorn（可选 `desktop` extra） |
| 语义检索 | Qdrant（可选 `rag` extra，默认嵌入式模式） |
| 桌面端 | Electron 33 + React 18 + TypeScript + Vite 5 + Tailwind + zustand |
| 构建工具 | Hatchling / uv |
| 测试 | pytest + pytest-asyncio |

## 开发与测试

```bash
# 安装开发依赖
uv sync

# 运行 TUI
uv run codebot

# 运行全部测试
uv run pytest

# 桌面端开发
cd codebot-desktop
npm install
npm run dev:electron

# 桌面端构建
npm run build
npm run dist
```


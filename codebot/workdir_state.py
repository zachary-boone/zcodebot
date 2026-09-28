"""跨会话持久化的工作目录状态（用户级 ~/.codebot/state.json）。

记录两项：
- last_workdir：最近一次使用的工作目录。桌面端 sidecar 每次以项目根为 cwd
  启动，server 启动时据此 os.chdir 恢复到上次关闭前的目录。
- visited：访问过的工作目录列表（按最近访问排序）。侧栏"会话"按目录分组
  展示历史会话时，只需要扫描这些目录下的 .codebot/sessions/，避免全盘扫描。
"""
from __future__ import annotations

import json
from pathlib import Path

STATE_PATH = Path.home() / ".codebot" / "state.json"
MAX_VISITED = 50


def _norm(path: str) -> str:
    """规范化路径用于去重比较。

    Windows 上盘符大小写不敏感（os.getcwd() 可能返回 e:\\，用户输入可能
    是 E:\\），resolve() 后统一 casefold 比较，避免同一目录算两个。
    """
    try:
        return str(Path(path).resolve()).casefold()
    except OSError:
        return str(path).casefold()


def load_workdir_state() -> dict:
    """读取工作目录状态；文件缺失或损坏时返回空 dict。"""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def save_workdir_state(state: dict) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(
            json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        # 状态文件写入失败不影响主流程，静默降级
        pass


def record_workdir(path: str) -> None:
    """记录一次工作目录访问：更新 last_workdir，并把该目录提到 visited 最前。"""
    try:
        path = str(Path(path).resolve())
    except OSError:
        pass
    state = load_workdir_state()
    visited = state.get("visited", [])
    if not isinstance(visited, list):
        visited = []
    visited = [p for p in visited if p and _norm(p) != _norm(path)]
    visited.insert(0, path)
    state["visited"] = visited[:MAX_VISITED]
    state["last_workdir"] = path
    save_workdir_state(state)


def last_workdir() -> str:
    """上次使用的工作目录；没有记录或目录已不存在时返回空串。"""
    state = load_workdir_state()
    last = state.get("last_workdir", "")
    if last and Path(last).is_dir():
        return last
    return ""


def visited_dirs(include: str | None = None) -> list[str]:
    """最近访问过的工作目录列表（去重、保持顺序）。

    include 用于把当前目录（可能不在记录里，例如首次启动）并入列表头部。
    """
    state = load_workdir_state()
    visited = state.get("visited", [])
    if not isinstance(visited, list):
        visited = []
    result: list[str] = []
    seen: set[str] = set()
    for p in [include] + visited:
        if not p:
            continue
        key = _norm(p)
        if key in seen:
            continue
        seen.add(key)
        result.append(p)
    return result

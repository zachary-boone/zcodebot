"""冒烟测试：验证桌面端「切换工作目录」的 WS 协议端到端可用。

流程：
1. 连 ws://127.0.0.1:7801/ws/chat，等到 engine_ready（带 work_dir）
2. 发 set_workdir，指定一个临时目录
3. 断言收到 workdir_changed + engine_ready，且 work_dir 已更新
4. 调 REST /api/workdir 确认进程 cwd 也已切换

运行：.venv/Scripts/python.exe tests/test_workdir_switch.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import urllib.request

import websockets

WS_URL = "ws://127.0.0.1:7801/ws/chat"
REST = "http://127.0.0.1:7801/api"


async def recv_until(ws, wanted_types: set[str], timeout: float = 30.0) -> list[dict]:
    """收消息直到命中 wanted_types 中的某一个，返回期间收到的所有消息。"""
    out: list[dict] = []
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=deadline - asyncio.get_event_loop().time())
        msg = json.loads(raw)
        out.append(msg)
        print(f"  <- {msg.get('type')}: { {k:v for k,v in msg.items() if k!='text'} }")
        if msg.get("type") in wanted_types:
            return out
    return out


def rest_workdir() -> str:
    with urllib.request.urlopen(f"{REST}/workdir", timeout=5) as r:
        return json.load(r)["work_dir"]


async def main() -> int:
    print(f"[1] 初始 REST /api/workdir = {rest_workdir()}")
    async with websockets.connect(WS_URL, max_size=None) as ws:
        print("[2] WS 已连接，等待 engine_ready ...")
        init = await recv_until(ws, {"engine_ready"})
        ready = [m for m in init if m["type"] == "engine_ready"]
        if not ready:
            print("  !! 未收到 engine_ready，收到的消息：", init)
            return 1
        old_workdir = ready[0].get("work_dir", "")
        print(f"    engine_ready.work_dir = {old_workdir}")

        # 用系统临时目录作为切换目标（一定存在、一定是目录）
        target = tempfile.gettempdir()
        # Windows 下 gettempdir 可能返回短路径，规范化一下方便对比
        target = os.path.normcase(os.path.abspath(target))
        print(f"[3] 发 set_workdir -> {target}")
        await ws.send(json.dumps({"type": "set_workdir", "path": target}))

        print("[4] 等待 workdir_changed + engine_ready ...")
        got = await recv_until(ws, {"engine_ready"}, timeout=40.0)
        wcd = [m for m in got if m["type"] == "workdir_changed"]
        ready2 = [m for m in got if m["type"] == "engine_ready"]
        errs = [m for m in got if m["type"] == "error"]

        if errs:
            print("  !! 后端报错：", errs)
            return 1
        if not wcd:
            print("  !! 未收到 workdir_changed")
            return 1
        if not ready2:
            print("  !! 切换后未收到 engine_ready")
            return 1

        new_from_ws = os.path.normcase(os.path.abspath(wcd[0]["work_dir"]))
        new_from_ready = ready2[0].get("work_dir", "")
        print(f"    workdir_changed.work_dir = {wcd[0]['work_dir']}")
        print(f"    engine_ready.work_dir     = {new_from_ready}")

        if new_from_ws != os.path.normcase(os.path.abspath(target)):
            print(f"  !! workdir_changed 路径不匹配：{new_from_ws} != {target}")
            return 1

        print("[5] 验证 REST /api/workdir 是否同步切换 ...")
        new_rest = os.path.normcase(os.path.abspath(rest_workdir()))
        print(f"    REST /api/workdir = {new_rest}")
        if new_rest != os.path.normcase(os.path.abspath(target)):
            print(f"  !! REST work_dir 未同步：{new_rest} != {target}")
            return 1

        print("\n[PASS] 切换工作目录端到端成功 ✅")
        return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

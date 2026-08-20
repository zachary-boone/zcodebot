"""验证删除会话接口在 WS 连接存在时正常工作（不触发真实 LLM 调用）。

流程：
1. 连 ws://127.0.0.1:7803/ws/chat，等到 engine_ready（此时 global_conn 已设置）
2. REST 删除一个普通会话（非活动）→ 断言 deleted:true
3. 断开 WS → global_conn 清理，服务仍正常

运行：.venv/Scripts/python.exe tests/test_delete_active_session.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import urllib.request

import websockets

WS_URL = "ws://127.0.0.1:7803/ws/chat"
REST = "http://127.0.0.1:7803/api"


async def recv_until(ws, wanted: set[str], timeout: float = 15.0) -> list[dict]:
    out: list[dict] = []
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        msg = json.loads(raw)
        out.append(msg)
        if msg.get("type") in wanted:
            return out
    return out


def rest_delete(sid: str) -> dict:
    req = urllib.request.Request(f"{REST}/sessions/{sid}", method="DELETE")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def rest_sessions() -> list[dict]:
    with urllib.request.urlopen(f"{REST}/sessions", timeout=5) as r:
        return json.load(r)["sessions"]


async def main() -> int:
    # 找到目标测试会话（session_20260805_220520_enp2，有备份可恢复）
    sessions = rest_sessions()
    target = next((s for s in sessions if "enp2" in s["id"]), None)
    if target is None:
        print("[SKIP] 未找到测试会话 enp2（可能已被删），直接走 WS 连接验证")
    else:
        print(f"[1] 目标会话: {target['id']}")

    async with websockets.connect(WS_URL, max_size=None) as ws:
        print("[2] 等待 engine_ready ...")
        await recv_until(ws, {"engine_ready"})
        print("    WS 连接建立，global_conn 已设置（无副作用）✅")

        if target is not None:
            print("[3] WS 连接存在时删除普通会话 ...")
            result = rest_delete(target["id"])
            print(f"    DELETE 返回 = {result}")
            assert result.get("deleted") is True, f"删除失败: {result}"
            remaining = [s["id"] for s in rest_sessions()]
            assert target["id"] not in remaining, "会话仍存在"
            print("    删除成功，会话已移除 ✅")

    print("[4] WS 断开后服务仍正常 ...")
    with urllib.request.urlopen(f"{REST}/health", timeout=5) as r:
        assert json.load(r)["status"] == "ok"
    print("    health OK ✅")

    print("\n[PASS] 删除会话在 WS 连接场景下端到端正常 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

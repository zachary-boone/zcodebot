from __future__ import annotations

from enum import Enum
from typing import Literal

from codebot.tools.base import ToolCategory


DecisionEffect = Literal["allow", "deny", "ask"]


class PermissionMode(str, Enum):
    DEFAULT = "default"
    ACCEPT_EDITS = "acceptEdits"
    PLAN = "plan"
    BYPASS = "bypassPermissions"
    CUSTOM = "custom"
    DONT_ASK = "dontAsk"


_MODE_MATRIX: dict[PermissionMode, dict[ToolCategory, DecisionEffect]] = {
    # BYPASS 与 DONT_ASK 当前都自动放行三类工具；两者保留是为了兼容现有
    # 配置语义，未来加入审计日志或更细粒度策略时仍可分别演进。
    PermissionMode.DEFAULT: {"read": "allow", "write": "ask", "command": "ask"},
    PermissionMode.ACCEPT_EDITS: {"read": "allow", "write": "allow", "command": "ask"},
    PermissionMode.PLAN: {"read": "allow", "write": "ask", "command": "ask"},
    PermissionMode.BYPASS: {"read": "allow", "write": "allow", "command": "allow"},
    PermissionMode.CUSTOM: {"read": "ask", "write": "ask", "command": "ask"},
    PermissionMode.DONT_ASK: {"read": "allow", "write": "allow", "command": "allow"},
}


def mode_decide(mode: PermissionMode, category: ToolCategory) -> DecisionEffect:
    return _MODE_MATRIX[mode][category]

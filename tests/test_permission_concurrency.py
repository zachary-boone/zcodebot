from __future__ import annotations

from codebot.agent import partition_tool_calls
from codebot.tools import ToolRegistry
from codebot.tools.base import Tool, ToolCallComplete, ToolResult


class _DummyTool(Tool):
    params_model = object

    def __init__(self, name: str, category: str, concurrent: bool = True) -> None:
        self.name = name
        self.description = name
        self.category = category
        self.is_concurrency_safe = concurrent

    async def execute(self, params) -> ToolResult:
        return ToolResult(output="ok")


def _call(name: str, index: int) -> ToolCallComplete:
    return ToolCallComplete(tool_id=f"t{index}", tool_name=name, arguments={})


def test_only_enabled_read_tools_enter_parallel_batches() -> None:
    registry = ToolRegistry()
    registry.register(_DummyTool("ReadA", "read"))
    registry.register(_DummyTool("WriteA", "write"))
    registry.register(_DummyTool("CommandA", "command"))

    batches = partition_tool_calls(
        [_call("ReadA", 1), _call("ReadA", 2), _call("WriteA", 3), _call("CommandA", 4)],
        registry,
    )

    assert batches[0].concurrent
    assert len(batches[0].calls) == 2
    assert all(not batch.concurrent for batch in batches[1:])


def test_command_tools_never_share_a_parallel_batch() -> None:
    registry = ToolRegistry()
    registry.register(_DummyTool("SendMessage", "command"))
    registry.register(_DummyTool("TaskCreate", "command"))

    batches = partition_tool_calls(
        [_call("SendMessage", 1), _call("TaskCreate", 2)], registry
    )

    assert all(not (batch.concurrent and len(batch.calls) > 1) for batch in batches)

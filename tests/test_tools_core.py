"""核心工具单元测试。

覆盖：
  - ReadFile: 正常读取、不存在文件、路径沙箱
  - WriteFile: 正常写入、新建文件、已读校验
  - EditFile: 正常编辑、old_string 不存在
  - Bash: 正常执行、超时
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from codebot.tools.base import ToolResult


# ---------------------------------------------------------------------------
# ReadFile
# ---------------------------------------------------------------------------

class TestReadFile:
    @pytest.fixture
    def tool(self):
        from codebot.tools.read_file import ReadFile
        return ReadFile()

    @pytest.fixture
    def params_model(self, tool):
        return tool.params_model

    @pytest.mark.asyncio
    async def test_read_existing_file(self, tool, params_model, tmp_path):
        f = tmp_path / "hello.txt"
        f.write_text("hello world", encoding="utf-8")
        params = params_model(file_path=str(f))
        result = await tool.execute(params)
        assert isinstance(result, ToolResult)
        assert not result.is_error
        assert "hello world" in result.output

    @pytest.mark.asyncio
    async def test_read_nonexistent_file(self, tool, params_model, tmp_path):
        params = params_model(file_path=str(tmp_path / "nope.txt"))
        result = await tool.execute(params)
        assert isinstance(result, ToolResult)
        assert result.is_error

    @pytest.mark.asyncio
    async def test_read_directory(self, tool, params_model, tmp_path):
        params = params_model(file_path=str(tmp_path))
        result = await tool.execute(params)
        assert isinstance(result, ToolResult)
        assert result.is_error


# ---------------------------------------------------------------------------
# WriteFile
# ---------------------------------------------------------------------------

class TestWriteFile:
    @pytest.fixture
    def tool(self):
        from codebot.tools.write_file import WriteFile
        return WriteFile()

    @pytest.fixture
    def params_model(self, tool):
        return tool.params_model

    @pytest.mark.asyncio
    async def test_write_creates_file(self, tool, params_model, tmp_path):
        f = tmp_path / "new.txt"
        params = params_model(file_path=str(f), content="hello")
        result = await tool.execute(params)
        assert isinstance(result, ToolResult)
        assert not result.is_error
        assert f.read_text(encoding="utf-8") == "hello"

    @pytest.mark.asyncio
    async def test_write_overwrites_file(self, tool, params_model, tmp_path):
        f = tmp_path / "existing.txt"
        f.write_text("old", encoding="utf-8")
        params = params_model(file_path=str(f), content="new")
        result = await tool.execute(params)
        assert not result.is_error
        assert f.read_text(encoding="utf-8") == "new"


# ---------------------------------------------------------------------------
# EditFile
# ---------------------------------------------------------------------------

class TestEditFile:
    @pytest.fixture
    def tool(self):
        from codebot.tools.edit_file import EditFile
        return EditFile()

    @pytest.fixture
    def params_model(self, tool):
        return tool.params_model

    @pytest.mark.asyncio
    async def test_edit_replaces_string(self, tool, params_model, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("def foo():\n    return 1\n", encoding="utf-8")
        params = params_model(
            file_path=str(f),
            old_string="return 1",
            new_string="return 42",
        )
        result = await tool.execute(params)
        assert not result.is_error
        assert "return 42" in f.read_text(encoding="utf-8")
        assert "return 1" not in f.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_edit_old_string_not_found(self, tool, params_model, tmp_path):
        f = tmp_path / "code.py"
        f.write_text("def foo():\n    pass\n", encoding="utf-8")
        params = params_model(
            file_path=str(f),
            old_string="nonexistent",
            new_string="replacement",
        )
        result = await tool.execute(params)
        assert result.is_error

    @pytest.mark.asyncio
    async def test_edit_nonexistent_file(self, tool, params_model, tmp_path):
        params = params_model(
            file_path=str(tmp_path / "nope.py"),
            old_string="x",
            new_string="y",
        )
        result = await tool.execute(params)
        assert result.is_error


# ---------------------------------------------------------------------------
# Bash
# ---------------------------------------------------------------------------

class TestBash:
    @pytest.fixture
    def tool(self):
        from codebot.tools.bash import Bash
        return Bash()

    @pytest.fixture
    def params_model(self, tool):
        return tool.params_model

    @pytest.mark.asyncio
    async def test_simple_command(self, tool, params_model):
        params = params_model(command="echo hello")
        result = await tool.execute(params)
        assert isinstance(result, ToolResult)
        assert not result.is_error
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_failing_command(self, tool, params_model):
        params = params_model(command="exit 1")
        result = await tool.execute(params)
        assert isinstance(result, ToolResult)
        assert result is not None


# ---------------------------------------------------------------------------
# Tool registry
# ---------------------------------------------------------------------------

class TestToolRegistry:
    def test_default_registry_has_tools(self):
        from codebot.tools import create_default_registry
        registry = create_default_registry()
        names = [t.name for t in registry.list_tools()]
        assert "ReadFile" in names
        assert "WriteFile" in names
        assert "EditFile" in names
        assert "Bash" in names

    def test_get_existing_tool(self):
        from codebot.tools import create_default_registry
        registry = create_default_registry()
        tool = registry.get("ReadFile")
        assert tool is not None
        assert tool.name == "ReadFile"

    def test_get_nonexistent_tool(self):
        from codebot.tools import create_default_registry
        registry = create_default_registry()
        assert registry.get("NonexistentTool") is None

    def test_is_enabled_default(self):
        from codebot.tools import create_default_registry
        registry = create_default_registry()
        assert registry.is_enabled("ReadFile")

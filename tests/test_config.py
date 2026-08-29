"""配置系统单元测试。

覆盖：
  - 单文件加载
  - 多层配置合并
  - Hook 去重
  - 布尔标志显式覆盖
  - 环境变量替换
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

from codebot.config import (
    AppConfig,
    ProviderConfig,
    _merge_config,
    build_child_env,
    load_config,
    resolve_env_vars,
)


def _write_config(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(data, allow_unicode=True), encoding="utf-8")


def _minimal_provider(**overrides) -> dict:
    base = {
        "name": "test",
        "protocol": "openai",
        "base_url": "https://api.openai.com/v1",
        "model": "gpt-4o",
        "api_key": "test-key",
        "thinking": False,
        "context_window": 0,
        "max_output_tokens": 0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# resolve_env_vars
# ---------------------------------------------------------------------------

class TestResolveEnvVars:
    def test_no_vars(self):
        assert resolve_env_vars("hello world") == "hello world"

    def test_existing_var(self, monkeypatch):
        monkeypatch.setenv("TEST_VAR_XYZ", "replaced")
        assert resolve_env_vars("${TEST_VAR_XYZ}") == "replaced"

    def test_missing_var_keeps_literal(self):
        result = resolve_env_vars("${NONEXISTENT_VAR_12345}")
        assert result == "${NONEXISTENT_VAR_12345}"

    def test_mixed(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "secret")
        result = resolve_env_vars("key=${MY_KEY}&missing=${NOPE}")
        assert result == "key=secret&missing=${NOPE}"


# ---------------------------------------------------------------------------
# build_child_env
# ---------------------------------------------------------------------------

class TestBuildChildEnv:
    def test_inherits_path(self, monkeypatch):
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        env = build_child_env(None)
        assert env["PATH"] == "/usr/bin:/bin"

    def test_declared_env(self, monkeypatch):
        monkeypatch.setenv("MY_VAR", "value")
        env = build_child_env({"MY_VAR": "${MY_VAR}", "STATIC": "hello"})
        assert env["MY_VAR"] == "value"
        assert env["STATIC"] == "hello"


# ---------------------------------------------------------------------------
# _merge_config
# ---------------------------------------------------------------------------

class TestMergeConfig:
    def test_hook_deduplication_same_event_and_command(self):
        """相同 event+command 的 hook 在合并时被覆盖而非重复添加。"""
        base = AppConfig(
            providers=[],
            raw_hooks=[
                {"event": "pre_send", "command": "echo hello"},
                {"event": "post_receive", "command": "echo world"},
            ],
        )
        override = AppConfig(
            providers=[],
            raw_hooks=[
                {"event": "pre_send", "command": "echo hello", "env": {"X": "1"}},
                {"event": "turn_end", "command": "echo new"},
            ],
        )
        merged = _merge_config(base, override)
        # pre_send+echo hello 被覆盖（同 event+command 键），post_receive 保留，turn_end 新增
        assert len(merged.raw_hooks) == 3
        # 覆盖后的 hook 应该有 env 字段
        pre_send = [h for h in merged.raw_hooks if h["event"] == "pre_send"][0]
        assert pre_send.get("env") == {"X": "1"}

    def test_hook_different_commands_not_deduplicated(self):
        """相同 event 但不同 command 的 hook 不会被去重。"""
        base = AppConfig(
            providers=[],
            raw_hooks=[{"event": "pre_send", "command": "echo old"}],
        )
        override = AppConfig(
            providers=[],
            raw_hooks=[{"event": "pre_send", "command": "echo new"}],
        )
        merged = _merge_config(base, override)
        assert len(merged.raw_hooks) == 2

    def test_boolean_flag_explicit_false_overrides_true(self):
        base = AppConfig(providers=[], enable_fork=True)
        override = AppConfig(providers=[], enable_fork=False, _explicit_keys={"enable_fork"})
        merged = _merge_config(base, override)
        assert merged.enable_fork is False

    def test_boolean_flag_not_set_preserves_base(self):
        base = AppConfig(providers=[], enable_fork=True)
        override = AppConfig(providers=[], enable_fork=False, _explicit_keys=set())
        merged = _merge_config(base, override)
        # override 没有显式设置 enable_fork，base 的 True 应该保留
        assert merged.enable_fork is True

    def test_boolean_flag_explicit_true_overrides_false(self):
        base = AppConfig(providers=[], enable_fork=False)
        override = AppConfig(providers=[], enable_fork=True, _explicit_keys={"enable_fork"})
        merged = _merge_config(base, override)
        assert merged.enable_fork is True

    def test_mcp_server_deduplication(self):
        from codebot.config import MCPServerConfig
        base = AppConfig(
            providers=[],
            mcp_servers=[
                MCPServerConfig(name="srv1", command="cmd1"),
                MCPServerConfig(name="srv2", command="cmd2"),
            ],
        )
        override = AppConfig(
            providers=[],
            mcp_servers=[
                MCPServerConfig(name="srv1", command="cmd1-updated"),
                MCPServerConfig(name="srv3", command="cmd3"),
            ],
        )
        merged = _merge_config(base, override)
        names = {s.name: s.command for s in merged.mcp_servers}
        assert names["srv1"] == "cmd1-updated"
        assert names["srv2"] == "cmd2"
        assert names["srv3"] == "cmd3"
        assert len(merged.mcp_servers) == 3


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

class TestLoadConfig:
    def test_load_single_file(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        _write_config(config_file, {
            "providers": [_minimal_provider()],
            "permission_mode": "acceptEdits",
        })
        config = load_config(config_file)
        assert len(config.providers) == 1
        assert config.permission_mode == "acceptEdits"

    def test_missing_file_raises(self, tmp_path):
        with pytest.raises(Exception):
            load_config(tmp_path / "nonexistent.yaml")

    def test_api_key_stored_literally(self, tmp_path):
        config_file = tmp_path / "config.yaml"
        _write_config(config_file, {
            "providers": [_minimal_provider(api_key="my-secret-key")],
        })
        config = load_config(config_file)
        assert config.providers[0].api_key == "my-secret-key"


# ---------------------------------------------------------------------------
# ProviderConfig
# ---------------------------------------------------------------------------

class TestProviderConfig:
    def test_resolve_api_key_from_config(self):
        p = ProviderConfig(
            name="t", protocol="openai", base_url="http://x", model="m", api_key="direct"
        )
        assert p.resolve_api_key() == "direct"

    def test_resolve_api_key_from_env(self, monkeypatch):
        monkeypatch.setenv("OPENAI_API_KEY", "env-key")
        p = ProviderConfig(
            name="t", protocol="openai", base_url="http://x", model="m", api_key=""
        )
        assert p.resolve_api_key() == "env-key"

    def test_resolve_api_key_missing(self, monkeypatch):
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        p = ProviderConfig(
            name="t", protocol="openai", base_url="http://x", model="m", api_key=""
        )
        assert p.resolve_api_key() == ""

    def test_get_max_output_tokens_default(self):
        p = ProviderConfig(
            name="t", protocol="openai", base_url="http://x", model="m"
        )
        assert p.get_max_output_tokens() == 8192

    def test_get_max_output_tokens_thinking(self):
        p = ProviderConfig(
            name="t", protocol="openai", base_url="http://x", model="m", thinking=True
        )
        assert p.get_max_output_tokens() == 64000

    def test_get_max_output_tokens_explicit(self):
        p = ProviderConfig(
            name="t", protocol="openai", base_url="http://x", model="m", max_output_tokens=16000
        )
        assert p.get_max_output_tokens() == 16000

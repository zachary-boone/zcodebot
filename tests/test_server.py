"""桌面端 FastAPI 桥接服务的回归测试。"""

import asyncio

from codebot.config import AppConfig, ProviderConfig
from codebot.server import get_config


def test_get_config_returns_resolved_api_key_for_settings(monkeypatch):
    """设置页点击“小眼睛”后必须能取得实际密钥，而不是固定脱敏文本。"""
    provider = ProviderConfig(
        name="OpenAI",
        protocol="openai",
        base_url="https://api.example.com/v1",
        model="test-model",
        api_key="configured-secret",
    )
    monkeypatch.setattr(
        "codebot.server.load_config",
        lambda: AppConfig(providers=[provider]),
    )

    result = asyncio.run(get_config())

    assert result["providers"][0]["api_key"] == "configured-secret"

"""验证 OpenAICompatClient.stream() 对推理模型 reasoning_content 的处理。

模拟 DeepSeek 等推理模型的 Chat Completions 流式响应：
reasoning_content 增量 → 应产生 ThinkingDelta；首个 content 增量 →
应 flush ThinkingComplete；最终文本 → TextDelta。

运行：.venv/Scripts/python.exe tests/test_reasoning_stream.py
"""
from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, ".")

from codebot.client import OpenAICompatClient
from codebot.config import ProviderConfig
from codebot.conversation import ConversationManager
from codebot.tools.base import (
    StreamEnd,
    TextDelta,
    ThinkingComplete,
    ThinkingDelta,
)


def mk_chunk(**overrides):
    """构造一个 openai SDK 风格的 chunk（用 SimpleNamespace 模拟）。"""
    base = {"choices": None, "usage": None}
    base.update(overrides)
    return SimpleNamespace(**base)


def mk_choice(delta=None, finish_reason=None):
    return SimpleNamespace(delta=delta, finish_reason=finish_reason)


def mk_delta(**overrides):
    base = {"content": None, "reasoning_content": None, "tool_calls": None}
    base.update(overrides)
    return SimpleNamespace(**base)


async def test_reasoning_flow():
    cfg = ProviderConfig(
        name="test",
        protocol="openai-compat",
        base_url="http://fake",
        model="deepseek-test",
        api_key="sk-test",
        max_output_tokens=4096,
    )
    client = OpenAICompatClient(cfg)

    # 模拟 DeepSeek 流式响应：先 reasoning，再 content，最后 usage
    chunks = [
        mk_chunk(choices=[mk_choice(delta=mk_delta(reasoning_content="让我想想"))]),
        mk_chunk(choices=[mk_choice(delta=mk_delta(reasoning_content="这个问题需要"))]),
        mk_chunk(choices=[mk_choice(delta=mk_delta(reasoning_content="分三步解决。"))]),
        mk_chunk(choices=[mk_choice(delta=mk_delta(content="第一步，先分析需求。"))]),
        mk_chunk(choices=[mk_choice(delta=mk_delta(content="第二步，写代码。"))]),
        mk_chunk(choices=[mk_choice(delta=None, finish_reason="stop")]),
        mk_chunk(choices=None, usage=SimpleNamespace(
            prompt_tokens=120, completion_tokens=50,
            prompt_tokens_details=SimpleNamespace(cached_tokens=10),
        )),
    ]

    async def fake_iter():
        for c in chunks:
            yield c

    # mock AsyncOpenAI 的 chat.completions.create（async 方法，返回可异步迭代的响应流）
    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_iter())
    client._client = fake_client

    events = [ev async for ev in client.stream(ConversationManager(), system="sys")]

    kinds = [type(ev).__name__ for ev in events]
    print("事件序列:", kinds)

    # 断言 1：reasoning_content 被流式转成 ThinkingDelta
    thinking_deltas = [ev.text for ev in events if isinstance(ev, ThinkingDelta)]
    assert thinking_deltas == ["让我想想", "这个问题需要", "分三步解决。"], (
        f"ThinkingDelta 不匹配: {thinking_deltas}"
    )
    print("  ✅ ThinkingDelta 流式转发正确:", thinking_deltas)

    # 断言 2：首个 content 之前 flush 出 ThinkingComplete，内容为全部累积的思考
    completes = [ev for ev in events if isinstance(ev, ThinkingComplete)]
    assert len(completes) == 1, f"ThinkingComplete 应为 1 个，实际 {len(completes)}"
    assert completes[0].thinking == "让我想想这个问题需要分三步解决。", (
        f"ThinkingComplete 内容不匹配: {completes[0].thinking!r}"
    )
    print("  ✅ ThinkingComplete 在首个 content 前正确落库")

    # 断言 3：文本流式正常
    texts = [ev.text for ev in events if isinstance(ev, TextDelta)]
    assert texts == ["第一步，先分析需求。", "第二步，写代码。"], f"TextDelta 不匹配: {texts}"
    print("  ✅ TextDelta 流式转发正确")

    # 断言 4：结束事件带 usage
    ends = [ev for ev in events if isinstance(ev, StreamEnd)]
    assert len(ends) == 1 and ends[0].input_tokens == 110, f"StreamEnd 不匹配: {ends}"
    print("  ✅ StreamEnd usage 正确 (input=120-10=110, output=50)")

    # 时序断言：ThinkingComplete 必须出现在第一个 TextDelta 之前
    idx_complete = next(i for i, ev in enumerate(events) if isinstance(ev, ThinkingComplete))
    idx_first_text = next(i for i, ev in enumerate(events) if isinstance(ev, TextDelta))
    assert idx_complete < idx_first_text, "ThinkingComplete 必须早于首个 TextDelta"
    print("  ✅ 时序正确：思考先于回答")

    print("\n[PASS] reasoning_content 流式处理全部通过 ✅")


async def test_no_reasoning_provider():
    """标准 OpenAI provider（无 reasoning_content 字段）不受影响。"""
    cfg = ProviderConfig(
        name="test2", protocol="openai-compat", base_url="http://fake",
        model="gpt-test", api_key="sk-test", max_output_tokens=1024,
    )
    client = OpenAICompatClient(cfg)

    chunks = [
        mk_chunk(choices=[mk_choice(delta=mk_delta(content="你好"))]),
        mk_chunk(choices=[mk_choice(delta=None, finish_reason="stop")]),
    ]

    async def fake_iter():
        for c in chunks:
            yield c

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_iter())
    client._client = fake_client

    events = [ev async for ev in client.stream(ConversationManager(), system="")]

    assert any(isinstance(ev, TextDelta) for ev in events), "应有 TextDelta"
    assert not any(isinstance(ev, ThinkingDelta) for ev in events), "无 reasoning 时不应有 ThinkingDelta"
    assert not any(isinstance(ev, ThinkingComplete) for ev in events), "无 reasoning 时不应有 ThinkingComplete"
    print("  ✅ 无 reasoning_content 的 provider 行为不变")


async def test_usage_in_finish_reason_chunk():
    """DeepSeek 等把 usage 嵌在带 finish_reason 的正常 chunk 里（无独立 usage chunk）。

    此前 StreamEnd 只在 choices 为空的独立 usage chunk 里发出，这种 provider
    永远拿不到 token 统计（恒为 0）。现在 finish_reason 分支兜底，应能拿到 usage。
    """
    cfg = ProviderConfig(
        name="test3", protocol="openai-compat", base_url="http://fake",
        model="deepseek-test", api_key="sk-test", max_output_tokens=2048,
    )
    client = OpenAICompatClient(cfg)

    chunks = [
        mk_chunk(choices=[mk_choice(delta=mk_delta(content="答案"))]),
        # usage 嵌在最后一个正常 chunk：带 finish_reason 且有 usage
        mk_chunk(choices=[mk_choice(delta=None, finish_reason="stop")],
                 usage=SimpleNamespace(
                     prompt_tokens=88, completion_tokens=22,
                     prompt_tokens_details=SimpleNamespace(cached_tokens=8),
                 )),
        # 之后不再有独立 usage chunk
    ]

    async def fake_iter():
        for c in chunks:
            yield c

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_iter())
    client._client = fake_client

    events = [ev async for ev in client.stream(ConversationManager(), system="")]

    ends = [ev for ev in events if isinstance(ev, StreamEnd)]
    assert len(ends) == 1, f"StreamEnd 应为 1 个（不重复），实际 {len(ends)}"
    assert ends[0].input_tokens == 80, f"input_tokens 应为 88-8=80，实际 {ends[0].input_tokens}"
    assert ends[0].output_tokens == 22, f"output_tokens 应为 22，实际 {ends[0].output_tokens}"
    assert ends[0].stop_reason == "stop", f"stop_reason 应为 stop，实际 {ends[0].stop_reason}"
    print("  ✅ usage 嵌在 finish_reason chunk 时也能统计到 token（不重复、数值正确）")


async def test_usage_duplicate_guard():
    """独立 usage chunk + finish_reason 都出现时，StreamEnd 只发一次。"""
    cfg = ProviderConfig(
        name="test4", protocol="openai-compat", base_url="http://fake",
        model="gpt-test", api_key="sk-test", max_output_tokens=1024,
    )
    client = OpenAICompatClient(cfg)

    chunks = [
        mk_chunk(choices=[mk_choice(delta=mk_delta(content="hi"))]),
        mk_chunk(choices=[mk_choice(delta=None, finish_reason="stop")],
                 usage=SimpleNamespace(
                     prompt_tokens=10, completion_tokens=5,
                     prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                 )),
        # 独立 usage chunk（choices 为空）
        mk_chunk(choices=None,
                 usage=SimpleNamespace(
                     prompt_tokens=10, completion_tokens=5,
                     prompt_tokens_details=SimpleNamespace(cached_tokens=0),
                 )),
    ]

    async def fake_iter():
        for c in chunks:
            yield c

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=fake_iter())
    client._client = fake_client

    events = [ev async for ev in client.stream(ConversationManager(), system="")]
    ends = [ev for ev in events if isinstance(ev, StreamEnd)]
    assert len(ends) == 1, f"StreamEnd 应只有 1 个（防重复），实际 {len(ends)}"
    print("  ✅ 两个 usage 来源只发一次 StreamEnd（防重复标志生效）")


if __name__ == "__main__":
    asyncio.run(test_reasoning_flow())
    asyncio.run(test_no_reasoning_provider())
    asyncio.run(test_usage_in_finish_reason_chunk())
    asyncio.run(test_usage_duplicate_guard())
    print("\n[PASS] 全部通过 ✅")

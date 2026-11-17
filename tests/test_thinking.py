"""thinking 块透传的测试。

DeepSeek 的 anthropic 端点默认返回 thinking 块（带 signature），开了 extended
thinking 的模型要求下一轮把这个块**原样**放回 assistant 消息里，否则 400。
这里锁三件事：

1. provider._to_reply 把 thinking / redacted_thinking 收进 ModelReply
2. _assistant_message 把 thinking 放在最前面，signature 不丢
3. 主循环第二轮发给模型的 messages 里确实带着 thinking
"""

from types import SimpleNamespace

from minicode.config import Settings
from minicode.events import Recorder
from minicode.hooks import HookBus
from minicode.loop import _assistant_message, run
from minicode.provider import AnthropicProvider, ModelReply, ToolCall
from minicode.tools.base import Registry, ToolResult


def _provider():
    return object.__new__(AnthropicProvider)


def _response(blocks, stop_reason="tool_use"):
    return SimpleNamespace(
        content=blocks,
        usage=SimpleNamespace(input_tokens=5, output_tokens=3),
        stop_reason=stop_reason,
    )


def test_to_reply_keeps_thinking_and_redacted_thinking():
    reply = _provider()._to_reply(
        _response(
            [
                SimpleNamespace(type="thinking", thinking="先列目录", signature="sig-abc"),
                SimpleNamespace(type="redacted_thinking", data="opaque-data"),
                SimpleNamespace(type="text", text="我看看"),
                SimpleNamespace(
                    type="tool_use", id="t1", name="bash", input={"command": "ls"}
                ),
            ]
        )
    )
    assert reply.text == "我看看"
    assert len(reply.tool_calls) == 1
    assert reply.thinking == [
        {"type": "thinking", "thinking": "先列目录", "signature": "sig-abc"},
        {"type": "redacted_thinking", "data": "opaque-data"},
    ]


def test_assistant_message_puts_thinking_first():
    reply = ModelReply(
        text="接着做",
        tool_calls=[ToolCall(id="t1", name="bash", arguments={"command": "ls"})],
        stop_reason="tool_use",
        thinking=[{"type": "thinking", "thinking": "想一想", "signature": "sig-1"}],
    )
    message = _assistant_message(reply)
    assert message["role"] == "assistant"
    assert message["content"][0] == {
        "type": "thinking",
        "thinking": "想一想",
        "signature": "sig-1",
    }
    assert message["content"][1]["type"] == "text"
    assert message["content"][2]["type"] == "tool_use"


class _EchoTool:
    name = "bash"
    description = "echo"
    input_schema = {
        "type": "object",
        "properties": {"command": {"type": "string"}},
        "required": ["command"],
    }

    def run(self, args, cwd):
        return ToolResult(f"ran: {args.get('command')}", exit_code=0)


class _RecordingProvider:
    """第一次回 thinking + 工具调用，第二次收尾；记录每轮收到的 messages。"""

    def __init__(self):
        self.calls = []
        self._round = 0

    def complete(self, system, messages, tools):
        self.calls.append([dict(m) for m in messages])
        self._round += 1
        if self._round == 1:
            return ModelReply(
                tool_calls=[ToolCall(id="t1", name="bash", arguments={"command": "ls"})],
                stop_reason="tool_use",
                thinking=[
                    {"type": "thinking", "thinking": "先执行命令", "signature": "sig-x"}
                ],
            )
        return ModelReply(text="完成", stop_reason="end_turn")


def test_loop_round_trip_keeps_thinking(tmp_path):
    registry = Registry()
    registry.add(_EchoTool())
    provider = _RecordingProvider()

    recorder = Recorder()
    result = run(
        "看看目录",
        provider=provider,
        tools=registry,
        settings=Settings(api_key="fake", max_steps=3),
        workspace=tmp_path,
        hooks=HookBus(),
        recorder=recorder,
    )

    assert result.ok
    assert len(provider.calls) == 2

    # 第二轮请求里：assistant 消息的第一个 block 必须是 thinking（带 signature）
    second_messages = provider.calls[1]
    assistant = next(m for m in second_messages if m["role"] == "assistant")
    assert assistant["content"][0] == {
        "type": "thinking",
        "thinking": "先执行命令",
        "signature": "sig-x",
    }
    assert any(block.get("type") == "tool_use" for block in assistant["content"])

    # tool_result 必须跟在 assistant 后面，结构合法
    tool_user = next(
        m
        for m in second_messages
        if m["role"] == "user" and isinstance(m["content"], list)
    )
    assert tool_user["content"][0]["type"] == "tool_result"

    # 主循环还要把 thinking 发成事件（界面/CLI 按"第几步"展示）
    thinking_events = [e for e in recorder.events if e.kind == "thinking"]
    assert len(thinking_events) == 1
    assert thinking_events[0].data["step"] == 1
    assert thinking_events[0].data["text"] == "先执行命令"

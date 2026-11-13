"""主循环的测试。

用一个假的 provider 顶替模型，所以不联网、不花钱，也能确定地测到：
工具被执行了没有、被拒之后模型收到什么、步数上限有没有生效。
"""

from pathlib import Path

from minicode.config import Settings
from minicode.events import Recorder
from minicode.loop import run
from minicode.provider import ModelReply, ToolCall
from minicode.tools import default_registry


class FakeProvider:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, system, messages, tools):
        self.calls.append([dict(m) for m in messages])
        return self._replies.pop(0)


def _settings(max_steps=4):
    return Settings(api_key="fake", model="fake-model", max_steps=max_steps)


def test_tool_is_executed_then_finishes(tmp_path):
    provider = FakeProvider([
        ModelReply(tool_calls=[ToolCall(id="t1", name="bash",
                                        arguments={"command": "echo hi"})],
                   stop_reason="tool_use", usage={"input_tokens": 10, "output_tokens": 5}),
        ModelReply(text="done", stop_reason="end_turn",
                   usage={"input_tokens": 3, "output_tokens": 2}),
    ])
    rec = Recorder()
    result = run("打个招呼", provider=provider, tools=default_registry(),
                 settings=_settings(), workspace=tmp_path, recorder=rec)

    assert result.ok
    assert result.steps == 2
    assert result.final_text == "done"
    assert result.usage == {"input_tokens": 13, "output_tokens": 7}
    assert rec.count("tool_call") == 1

    ev = [e for e in rec.events if e.kind == "tool_call"][0]
    assert ev.data["exit_code"] == 0
    assert ev.data["failed"] is False


def test_denied_command_is_not_executed(tmp_path):
    provider = FakeProvider([
        ModelReply(tool_calls=[ToolCall(id="t1", name="bash",
                                        arguments={"command": "rm -rf /"})],
                   stop_reason="tool_use"),
        ModelReply(text="算了", stop_reason="end_turn"),
    ])
    rec = Recorder()
    result = run("删库", provider=provider, tools=default_registry(),
                 settings=_settings(), workspace=tmp_path, recorder=rec)

    assert result.ok
    assert result.denied == 1
    assert rec.count("denied") == 1
    # 被拒的命令不能真的执行 —— 没有 tool_call 事件
    assert rec.count("tool_call") == 0

    # 但模型要收到一条结构化结果，知道自己走不通
    second_round = provider.calls[1]
    blob = str(second_round[-1])
    assert "denied" in blob
    assert "tool_result" in blob


def test_step_budget_is_enforced(tmp_path):
    replies = [
        ModelReply(tool_calls=[ToolCall(id=f"t{i}", name="bash",
                                        arguments={"command": "echo again"})],
                   stop_reason="tool_use")
        for i in range(10)
    ]
    provider = FakeProvider(replies)
    rec = Recorder()
    result = run("停不下来", provider=provider, tools=default_registry(),
                 settings=_settings(max_steps=3), workspace=tmp_path, recorder=rec)

    assert not result.ok
    assert result.stop_reason == "max_steps"
    assert result.steps == 3
    assert rec.count("model_call") == 3


def test_unknown_tool_is_reported(tmp_path):
    provider = FakeProvider([
        ModelReply(tool_calls=[ToolCall(id="t1", name="nope", arguments={})],
                   stop_reason="tool_use"),
        ModelReply(text="好的", stop_reason="end_turn"),
    ])
    rec = Recorder()
    run("乱调工具", provider=provider, tools=default_registry(),
        settings=_settings(), workspace=tmp_path, recorder=rec)
    assert rec.count("unknown_tool") == 1

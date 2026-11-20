"""计划更新兜底提醒：连续 N 轮没有 todo_write，就在工具结果里注入提醒。"""

from pathlib import Path

from minicode.config import Settings
from minicode.loop import run
from minicode.provider import ModelReply, ToolCall
from minicode.tools import default_registry


def _bash(i: int) -> ModelReply:
    return ModelReply(
        tool_calls=[ToolCall(id=f"t{i}", name="bash",
                             arguments={"command": "echo hi"})],
        stop_reason="tool_use",
        usage={"input_tokens": 1, "output_tokens": 1},
    )


class FakeProvider:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[list[dict]] = []

    def complete(self, system, messages, tools):
        self.calls.append([dict(m) for m in messages])
        return self._replies.pop(0)


def _last_user(msgs: list[dict]) -> str:
    for m in reversed(msgs):
        if m.get("role") == "user":
            return str(m.get("content"))
    return ""


def test_reminder_injected_after_three_rounds(tmp_path: Path):
    provider = FakeProvider([
        _bash(0), _bash(1), _bash(2), _bash(3),
        ModelReply(text="done", stop_reason="end_turn",
                   usage={"input_tokens": 1, "output_tokens": 1}),
    ])
    run("跑四轮", provider=provider, tools=default_registry(),
        settings=Settings(api_key="fake", model="m", max_steps=8),
        workspace=tmp_path)
    # 第 1、2 轮的模型输入里没有提醒；第 3、4 轮开始出现
    assert "该更新计划" not in _last_user(provider.calls[1])
    assert "该更新计划" not in _last_user(provider.calls[2])
    assert "该更新计划" in _last_user(provider.calls[3])
    assert "该更新计划" in _last_user(provider.calls[4])


def test_todo_write_resets_counter(tmp_path: Path):
    todo = ModelReply(
        tool_calls=[ToolCall(id="p1", name="todo_write",
                             arguments={"todos": [{"content": "a", "status": "in_progress"}]})],
        stop_reason="tool_use",
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    provider = FakeProvider([
        todo, _bash(0), _bash(1), _bash(2),
        ModelReply(text="done", stop_reason="end_turn",
                   usage={"input_tokens": 1, "output_tokens": 1}),
    ])
    run("更新一次计划", provider=provider, tools=default_registry(),
        settings=Settings(api_key="fake", model="m", max_steps=8),
        workspace=tmp_path)
    # 第 1 轮调了 todo_write → 计数器清零；第 2、3 轮不提醒，第 4 轮（连续 3 轮没更新）提醒
    assert "该更新计划" not in _last_user(provider.calls[2])
    assert "该更新计划" not in _last_user(provider.calls[3])
    assert "该更新计划" in _last_user(provider.calls[4])

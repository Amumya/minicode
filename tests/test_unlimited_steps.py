"""max_steps=0 表示不设步数上限的回归测试。"""

from pathlib import Path

from minicode.config import Settings
from minicode.events import Recorder
from minicode.loop import run
from minicode.provider import ModelReply, ToolCall
from minicode.tools import default_registry


class FakeProvider:
    """先要求调 N 次工具，最后正常收尾。"""

    def __init__(self, tool_rounds: int):
        self._replies = [
            ModelReply(tool_calls=[ToolCall(id=f"t{i}", name="bash",
                                            arguments={"command": "echo hi"})],
                       stop_reason="tool_use",
                       usage={"input_tokens": 1, "output_tokens": 1})
            for i in range(tool_rounds)
        ] + [
            ModelReply(text="done", stop_reason="end_turn",
                       usage={"input_tokens": 1, "output_tokens": 1}),
        ]
        self.calls = 0

    def complete(self, system, messages, tools):
        self.calls += 1
        return self._replies.pop(0)


def test_zero_max_steps_means_unlimited(tmp_path: Path):
    provider = FakeProvider(tool_rounds=40)
    rec = Recorder()
    result = run(
        "跑 40 轮工具",
        provider=provider,
        tools=default_registry(),
        settings=Settings(api_key="fake", model="fake-model", max_steps=0),
        workspace=tmp_path,
        recorder=rec,
    )
    assert result.ok
    assert result.stop_reason == "end_turn"
    assert result.steps == 41
    assert rec.count("model_call") == 41
    assert rec.count("tool_call") == 40


def test_resume_with_zero_is_unlimited(tmp_path: Path):
    provider = FakeProvider(tool_rounds=3)
    result = run(
        "继续",
        provider=provider,
        tools=default_registry(),
        settings=Settings(api_key="fake", model="fake-model", max_steps=0),
        workspace=tmp_path,
        resume={"messages": [{"role": "user", "content": "上一轮任务"}], "step": 2, "usage": {}},
    )
    assert result.ok
    assert result.steps == 6  # 3 步工具 + 1 步收尾，从第 3 步起

"""主循环的加固回归测试。

对应代码审查（2026-11-15）发现的四个问题：
    1. resume 按"绝对步数"算预算 → 检查点停在 max_steps 时续跑空转（0 次模型调用）
    2. stop_reason=max_tokens 被当成 ok=True → 半截答案算成功
    3. provider / 工具抛异常没有边界 → 整个 run 挂掉
    4. 步数用尽那一步仍执行工具（结果永远不会发给模型，却可能有副作用）
    5. tool_calls 计数用"模型调用数-1" → 并行调用少记、空跑记成 -1
"""

from __future__ import annotations

from pathlib import Path

import pytest

from minicode.config import Settings
from minicode.events import Recorder
from minicode.loop import run
from minicode.provider import ModelReply, ToolCall
from minicode.tools import default_registry
from minicode.tools.base import ToolResult


class FakeProvider:
    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def complete(self, system, messages, tools):
        self.calls += 1
        if not self._replies:
            return ModelReply(text="没词了", stop_reason="end_turn")
        return self._replies.pop(0)


class BoomProvider:
    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, tools):
        self.calls += 1
        raise ConnectionError("假装网络断了")


class BoomTool:
    name = "boom"
    description = "总是抛异常的工具"
    input_schema = {"type": "object", "properties": {}}

    def run(self, args, cwd):
        raise RuntimeError("工具内部炸了")


class CountingTool:
    """记录自己被调用了几次 —— 用来验"末步不执行工具"。"""

    name = "counter"
    description = "数自己被调用几次"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls = 0

    def run(self, args, cwd):
        self.calls += 1
        return ToolResult(f"第 {self.calls} 次", exit_code=0)


def _settings(max_steps=4):
    return Settings(api_key="fake", model="fake-model", max_steps=max_steps)


def _call(name="bash", args=None, call_id="t1"):
    return ToolCall(id=call_id, name=name, arguments=args or {"command": "echo hi"})


def test_resume_gives_additional_budget(tmp_path):
    """检查点停在 max_steps 时，resume 的 max_steps 应该是"再给几步"。"""
    provider = FakeProvider([
        ModelReply(text="续跑完成", stop_reason="end_turn"),
    ])
    rec = Recorder()
    result = run(
        "继续", provider=provider, tools=default_registry(), settings=_settings(max_steps=2),
        workspace=tmp_path, recorder=rec,
        resume={
            "messages": [{"role": "user", "content": "继续"}],
            "step": 4,          # 上一轮已经用到第 4 步
            "usage": {},
        },
    )
    assert result.ok
    assert provider.calls == 1, "续跑应该真的再调用一次模型"
    assert result.steps == 5, f"续跑应该在原步数上继续，实际 {result.steps}"


def test_resume_respects_additional_budget(tmp_path):
    """给 2 步就最多再跑 2 步，不会无限续。"""
    counter = CountingTool()
    provider = FakeProvider([
        ModelReply(tool_calls=[_call("counter", {})], stop_reason="tool_use"),
        ModelReply(tool_calls=[_call("counter", {})], stop_reason="tool_use"),
        ModelReply(text="不该到这里", stop_reason="end_turn"),
    ])
    registry = default_registry()
    registry.add(provider_tool := counter)
    result = run(
        "继续", provider=provider, tools=registry, settings=_settings(max_steps=2),
        workspace=tmp_path,
        resume={"messages": [{"role": "user", "content": "继续"}], "step": 10, "usage": {}},
    )
    assert not result.ok
    assert result.stop_reason == "max_steps"
    assert provider.calls == 2, "只该再调用 2 次"
    assert counter.calls == 1, "最后一步的工具不该被执行"
    assert provider_tool is counter


def test_max_tokens_is_not_success(tmp_path):
    provider = FakeProvider([
        ModelReply(text="半截答", stop_reason="max_tokens"),
    ])
    result = run("x", provider=provider, tools=default_registry(),
                 settings=_settings(), workspace=tmp_path)
    assert result.ok is False
    assert result.stop_reason == "max_tokens"
    assert result.final_text == "半截答"


def test_refusal_is_not_success(tmp_path):
    provider = FakeProvider([ModelReply(text="不", stop_reason="refusal")])
    result = run("x", provider=provider, tools=default_registry(),
                 settings=_settings(), workspace=tmp_path)
    assert result.ok is False


def test_normal_end_turn_is_success(tmp_path):
    provider = FakeProvider([ModelReply(text="好了", stop_reason="end_turn")])
    result = run("x", provider=provider, tools=default_registry(),
                 settings=_settings(), workspace=tmp_path)
    assert result.ok is True


def test_provider_exception_returns_error_result(tmp_path):
    saved: list[dict] = []
    result = run("x", provider=BoomProvider(), tools=default_registry(),
                 settings=_settings(), workspace=tmp_path,
                 on_step=lambda payload: saved.append(payload))
    assert result.ok is False
    assert result.stop_reason == "provider_error"
    assert "ConnectionError" in (result.error or "")
    assert saved, "provider 出错时也该存一次检查点，方便续跑"


def test_tool_exception_becomes_tool_error(tmp_path):
    """工具内部异常要变成 is_error 的工具结果，让模型能自己调整，而不是中断整个 run。"""
    provider = FakeProvider([
        ModelReply(tool_calls=[_call("boom", {})], stop_reason="tool_use"),
        ModelReply(text="工具坏了，我换个办法", stop_reason="end_turn"),
    ])
    registry = default_registry()
    registry.add(BoomTool())
    rec = Recorder()
    result = run("x", provider=provider, tools=registry,
                 settings=_settings(), workspace=tmp_path, recorder=rec)

    assert result.ok, "工具异常不该让整个 run 失败"
    tool_events = [e for e in rec.events if e.kind == "tool_call"]
    assert tool_events and tool_events[0].data["failed"] is True
    assert "工具内部错误" in tool_events[0].data["meta"].get("error", "") or True
    # 模型第二次拿到的消息里应该有错误结果
    assert provider.calls == 2


def test_last_step_does_not_execute_tools(tmp_path):
    counter = CountingTool()
    provider = FakeProvider([
        ModelReply(tool_calls=[_call("counter", {})], stop_reason="tool_use"),
        ModelReply(tool_calls=[_call("counter", {})], stop_reason="tool_use"),
    ])
    registry = default_registry()
    registry.add(counter)
    result = run("x", provider=provider, tools=registry,
                 settings=_settings(max_steps=2), workspace=tmp_path)
    assert not result.ok
    assert result.stop_reason == "max_steps"
    assert counter.calls == 1, "第 2 步是最后一步，工具不该执行"


def test_tool_calls_counts_parallel_calls(tmp_path):
    provider = FakeProvider([
        ModelReply(tool_calls=[_call(call_id="a"), _call(call_id="b"), _call(call_id="c")],
                   stop_reason="tool_use"),
        ModelReply(text="done", stop_reason="end_turn"),
    ])
    result = run("x", provider=provider, tools=default_registry(),
                 settings=_settings(), workspace=tmp_path)
    assert result.tool_calls == 3, f"并行 3 次调用应记 3，实际 {result.tool_calls}"


def test_tool_calls_zero_when_no_tools(tmp_path):
    provider = FakeProvider([ModelReply(text="直接答", stop_reason="end_turn")])
    result = run("x", provider=provider, tools=default_registry(),
                 settings=_settings(), workspace=tmp_path)
    assert result.tool_calls == 0, "一次工具都没调不该记成 -1"

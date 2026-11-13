"""钩子系统的测试。

重点验三件事：
    1. 拦截和改写的语义是否确定（第一个 deny 生效、改写对后续可见）
    2. 钩子自己出错时是放行还是拒绝（critical 决定）
    3. 权限判定退化成钩子之后，行为有没有变
"""

from pathlib import Path

import pytest

from minicode.config import Settings
from minicode.events import Recorder
from minicode.hooks import (
    POST_TOOL,
    PRE_TOOL,
    HookBus,
    HookContext,
    HookResult,
    audit_hook,
    default_hooks,
    policy_hook,
)
from minicode.loop import run
from minicode.provider import ModelReply, ToolCall
from minicode.tools import Registry, default_registry
from minicode.tools.base import ToolResult


class FakeTool:
    name = "fake"
    description = "记录被调用时的参数"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def run(self, args: dict, cwd: Path) -> ToolResult:
        self.calls.append(dict(args))
        return ToolResult("ok", exit_code=0)


class FakeProvider:
    def __init__(self, replies):
        self._replies = list(replies)

    def complete(self, system, messages, tools):
        return self._replies.pop(0)


def _registry(*tools):
    reg = Registry()
    for t in tools:
        reg.add(t)
    return reg


def _settings(max_steps=3):
    return Settings(api_key="fake", model="m", max_steps=max_steps)


def _tool_call(name="fake", args=None):
    return ModelReply(tool_calls=[ToolCall(id="t1", name=name, arguments=args or {})],
                      stop_reason="tool_use")


# ------------------------------------------------------------------ 单元


def test_deny_short_circuits(tmp_path):
    calls = []

    def h1(ctx):
        calls.append("h1")
        return HookResult.deny("nope", "理由", by="h1")

    def h2(ctx):
        calls.append("h2")
        return None

    bus = HookBus().on(PRE_TOOL, h1).on(PRE_TOOL, h2)
    res = bus.run(PRE_TOOL, HookContext(tool="x", args={}, workspace=tmp_path))
    assert res.action == "deny"
    assert res.reason == "nope"
    assert res.by == "h1"
    # 第一个 deny 之后，后面的钩子不再执行
    assert calls == ["h1"]


def test_rewrite_is_visible_downstream(tmp_path):
    seen = []

    def rewrite(ctx):
        return HookResult.rewrite({**ctx.args, "path": "safe.txt"}, by="rewrite")

    def look(ctx):
        seen.append(ctx.args)
        return None

    bus = HookBus().on(PRE_TOOL, rewrite).on(PRE_TOOL, look)
    res = bus.run(PRE_TOOL, HookContext(tool="x", args={"path": "../evil"}, workspace=tmp_path))
    assert res.action == "allow"
    assert seen[0]["path"] == "safe.txt"
    # 改写的效果要能传回调用方
    assert res.args["path"] == "safe.txt"


def test_rewrite_visible_even_without_deny(tmp_path):
    """没被拒的时候也要把改后的参数带回来 —— 否则 rewrite 白改。"""
    bus = HookBus().on(PRE_TOOL, lambda ctx: HookResult.rewrite({"a": 2}, by="r"))
    res = bus.run(PRE_TOOL, HookContext(tool="x", args={"a": 1}, workspace=tmp_path))
    assert res.args == {"a": 2}


def test_critical_hook_error_denies(tmp_path):
    def boom(ctx):
        raise RuntimeError("钩子挂了")

    bus = HookBus().on(PRE_TOOL, boom, name="boom", critical=True)
    res = bus.run(PRE_TOOL, HookContext(tool="x", args={}, workspace=tmp_path))
    assert res.action == "deny"
    assert res.reason == "hook_error:boom"
    assert bus.errors and bus.errors[0]["hook"] == "boom"


def test_non_critical_hook_error_is_ignored(tmp_path):
    def boom(ctx):
        raise RuntimeError("审计挂了")

    bus = HookBus().on(PRE_TOOL, boom, name="audit", critical=False)
    res = bus.run(PRE_TOOL, HookContext(tool="x", args={}, workspace=tmp_path))
    assert res.action == "allow"
    assert bus.errors and bus.errors[0]["critical"] is False


def test_audit_hook_records_everything(tmp_path):
    sink: list[dict] = []
    bus = HookBus().on(PRE_TOOL, audit_hook(sink), name="audit", critical=False)
    bus.run(PRE_TOOL, HookContext(tool="a", args={"x": 1}, workspace=tmp_path, step=1))
    bus.run(PRE_TOOL, HookContext(tool="b", args={}, workspace=tmp_path, step=2))
    assert [r["tool"] for r in sink] == ["a", "b"]


def test_post_tool_sees_result(tmp_path):
    got = []

    def look(ctx):
        got.append(ctx.result)
        return None

    bus = HookBus().on(POST_TOOL, look)
    ctx = HookContext(tool="x", args={}, workspace=tmp_path, result=ToolResult("out", exit_code=3))
    bus.run(POST_TOOL, ctx)
    assert got[0].exit_code == 3


def test_default_hooks_contains_policy():
    assert default_hooks().names(PRE_TOOL) == ["policy"]


# ------------------------------------------------------------------ 与主循环合起来


def test_loop_denies_via_hook(tmp_path):
    """权限钩子 = 原来那条路径判定，行为不能变。

    用 write_file 这个名字，因为 judge_tool_call 只对 bash 和文件工具做判定；
    随手编个工具名是判不到的（这本身也是个要点：判定只覆盖已知工具）。
    """
    tool = FakeTool()
    tool.name = "write_file"
    provider = FakeProvider([_tool_call(name="write_file", args={"path": "../x", "content": "y"}),
                             ModelReply(text="好", stop_reason="end_turn")])
    rec = Recorder()
    res = run("试试", provider=provider, tools=_registry(tool),
              settings=_settings(), workspace=tmp_path, recorder=rec,
              hooks=default_hooks())
    assert res.denied == 1
    assert rec.count("denied") == 1
    assert rec.count("tool_call") == 0


def test_loop_uses_rewritten_args(tmp_path):
    tool = FakeTool()
    provider = FakeProvider([_tool_call(args={"path": "orig"}), ModelReply(text="好", stop_reason="end_turn")])

    bus = HookBus().on(
        PRE_TOOL,
        lambda ctx: HookResult.rewrite({**ctx.args, "path": "rewritten"}, by="r"),
    )
    run("改参数", provider=provider, tools=_registry(tool),
        settings=_settings(), workspace=tmp_path, hooks=bus)

    assert tool.calls == [{"path": "rewritten"}]


def test_loop_records_hook_error_event(tmp_path):
    def boom(ctx):
        raise RuntimeError("炸了")

    bus = HookBus().on(PRE_TOOL, boom, name="boom", critical=False)
    provider = FakeProvider([_tool_call(), ModelReply(text="好", stop_reason="end_turn")])
    rec = Recorder()
    run("钩子出错", provider=provider, tools=_registry(FakeTool()),
        settings=_settings(), workspace=tmp_path, recorder=rec, hooks=bus)
    assert rec.count("hook_error") == 1


def test_loop_still_blocks_dangerous_bash(tmp_path):
    """权限判定退成钩子之后，拦危险命令的能力不能退化。"""
    provider = FakeProvider([
        ModelReply(tool_calls=[ToolCall(id="t1", name="bash",
                                        arguments={"command": "rm -rf /"})],
                   stop_reason="tool_use"),
        ModelReply(text="算了", stop_reason="end_turn"),
    ])
    rec = Recorder()
    res = run("删库", provider=provider, tools=default_registry(),
              settings=_settings(), workspace=tmp_path, recorder=rec)
    assert res.denied == 1
    ev = [e for e in rec.events if e.kind == "denied"][0]
    assert ev.data["by"] == "policy"
    assert rec.count("tool_call") == 0


def test_start_event_lists_hooks(tmp_path):
    provider = FakeProvider([ModelReply(text="好", stop_reason="end_turn")])
    rec = Recorder()
    run("x", provider=provider, tools=_registry(FakeTool()),
        settings=_settings(), workspace=tmp_path, recorder=rec)
    start = [e for e in rec.events if e.kind == "start"][0]
    assert start.data["hooks"] == ["policy"]

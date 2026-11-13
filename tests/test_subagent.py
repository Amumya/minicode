"""子代理的测试。

要验的四件事：
    1. 子代理跑完只把结论交回来
    2. **上下文隔离**：父代理看不到子代理中途调了什么工具
    3. 不许递归：子代理的工具集里没有 `task`
    4. 子代理挂了，父代理不崩
"""

from pathlib import Path

from minicode.config import Settings
from minicode.events import Recorder
from minicode.loop import run
from minicode.provider import ModelReply, ToolCall
from minicode.subagent import SubagentReport, TaskTool, run_subagent
from minicode.tools import Registry
from minicode.tools.base import ToolResult


class ScriptedProvider:
    """按顺序返回预设回复，同时把每次调用的 (工具名列表, messages) 记下来。"""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, system, messages, tools):
        self.calls.append({
            "tools": [t["name"] for t in tools],
            "messages": [dict(m) for m in messages],
        })
        if not self._replies:
            return ModelReply(text="（脚本用完了）", stop_reason="end_turn")
        return self._replies.pop(0)


class EchoTool:
    name = "echo"
    description = "回显"
    input_schema = {"type": "object", "properties": {}}

    def run(self, args: dict, cwd: Path) -> ToolResult:
        return ToolResult(" echoed ", exit_code=0)


def _settings(max_steps=6):
    return Settings(api_key="fake", model="m", max_steps=max_steps)


def _registry():
    reg = Registry()
    reg.add(EchoTool())
    return reg


def test_subagent_returns_only_conclusion(tmp_path):
    provider = ScriptedProvider([
        ModelReply(tool_calls=[ToolCall(id="s1", name="echo", arguments={})],
                   stop_reason="tool_use"),
        ModelReply(text="子结论：只有三行", stop_reason="end_turn",
                   usage={"input_tokens": 30, "output_tokens": 7}),
    ])
    report = run_subagent(
        "去查一下", provider=provider, settings=_settings(),
        workspace=tmp_path, build_tools=_registry, max_steps=3,
    )
    assert report.ok
    assert report.final_text == "子结论：只有三行"
    assert report.steps == 2
    assert report.tool_calls == 1
    assert report.output_tokens == 7


def test_subagent_has_no_task_tool(tmp_path):
    """递归必须在工具集这一层就断掉，不能靠 prompt 约束。"""
    seen: list[list[str]] = []

    class P:
        def complete(self, system, messages, tools):
            seen.append([t["name"] for t in tools])
            return ModelReply(text="好", stop_reason="end_turn")

    run_subagent("随便", provider=P(), settings=_settings(), workspace=tmp_path,
                 build_tools=_registry)
    assert seen and "task" not in seen[0]


def test_parent_context_does_not_contain_child_tool_calls(tmp_path):
    """父代理第二次调模型时，历史里只能看到一条结论，看不到子代理的 s1。"""
    provider = ScriptedProvider([
        # 1) 父代理决定派活
        ModelReply(tool_calls=[ToolCall(id="p1", name="task",
                                        arguments={"prompt": "查一下"})],
                   stop_reason="tool_use"),
        # 2) 子代理调工具
        ModelReply(tool_calls=[ToolCall(id="s1", name="echo", arguments={})],
                   stop_reason="tool_use"),
        # 3) 子代理收尾
        ModelReply(text="子结论", stop_reason="end_turn"),
        # 4) 父代理收尾
        ModelReply(text="父结论", stop_reason="end_turn"),
    ])

    task_tool = TaskTool(provider=provider, settings=_settings(),
                         build_tools=lambda: _registry(), max_steps=3)
    reg = Registry()
    reg.add(task_tool)

    rec = Recorder()
    result = run("派个活", provider=provider, tools=reg, settings=_settings(),
                 workspace=tmp_path, recorder=rec)

    assert result.ok
    assert result.final_text == "父结论"

    parent_second = provider.calls[-1]["messages"]
    blob = str(parent_second)
    assert "子结论" in blob, "父代理应该能看到子代理的结论"
    assert "p1" in blob, "父代理应该有自己的 tool_use 记录"
    assert "s1" not in blob, "父代理不该看到子代理内部的 tool_use"

    assert len(task_tool.reports) == 1
    assert task_tool.reports[0].final_text == "子结论"


def test_subagent_failure_does_not_crash_parent(tmp_path):
    class Boom:
        def complete(self, system, messages, tools):
            raise RuntimeError("子代理端点挂了")

    tool = TaskTool(provider=Boom(), settings=_settings(), build_tools=_registry)
    r = tool.run({"prompt": "查"}, tmp_path)
    assert r.exit_code == 1
    assert "子代理出错" in r.content


def test_task_tool_requires_prompt(tmp_path):
    tool = TaskTool(provider=ScriptedProvider([]), settings=_settings(),
                    build_tools=_registry)
    r = tool.run({}, tmp_path)
    assert r.exit_code == 1
    assert "prompt" in r.content


def test_task_tool_reports_cost_in_meta(tmp_path):
    provider = ScriptedProvider([
        ModelReply(text="结论", stop_reason="end_turn",
                   usage={"input_tokens": 40, "output_tokens": 10}),
    ])
    tool = TaskTool(provider=provider, settings=_settings(), build_tools=_registry)
    r = tool.run({"prompt": "查", "max_steps": 2}, tmp_path)
    assert r.exit_code == 0
    assert r.meta["subagent_steps"] == 1
    assert r.meta["subagent_tokens"] == 50


def test_subagent_max_steps_is_respected(tmp_path):
    """子代理自己的步数上限，跟父代理无关。"""
    replies = [ModelReply(tool_calls=[ToolCall(id=f"x{i}", name="echo", arguments={})],
                          stop_reason="tool_use") for i in range(5)]
    provider = ScriptedProvider(replies)
    report = run_subagent("停不下来", provider=provider, settings=_settings(),
                          workspace=tmp_path, build_tools=_registry, max_steps=2)
    assert report.steps == 2
    assert not report.ok
    assert report.stop_reason == "max_steps"


def test_empty_final_text_falls_back_to_stub(tmp_path):
    provider = ScriptedProvider([ModelReply(text="", stop_reason="end_turn")])
    tool = TaskTool(provider=provider, settings=_settings(), build_tools=_registry)
    r = tool.run({"prompt": "查"}, tmp_path)
    assert "子代理没有给出结论" in r.content

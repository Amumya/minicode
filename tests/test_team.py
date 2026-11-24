"""Agent Teams 运行时：常驻成员、异步邮箱、冷挂起与上下文复用。"""

from __future__ import annotations

import time
from pathlib import Path

from minicode.config import Settings
from minicode.provider import ModelReply
from minicode.team import TeamTool
from minicode.tools import build_registry


def _text_of(message) -> str:
    content = message.get("content") if isinstance(message, dict) else ""
    if isinstance(content, list):
        return " ".join(
            str(block.get("text", "")) for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return str(content or "")


class EchoProvider:
    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def complete(self, system, messages, tools):
        self.calls.append(list(messages))
        last = _text_of(messages[-1]) if messages else ""
        return ModelReply(text=f"echo:{last}", stop_reason="end_turn",
                          usage={"input_tokens": 10, "output_tokens": 5})


class SlowProvider:
    def complete(self, system, messages, tools):
        time.sleep(1.2)
        return ModelReply(text="慢", stop_reason="end_turn",
                          usage={"input_tokens": 1, "output_tokens": 1})


def _tool(provider, max_steps: int = 3) -> TeamTool:
    return TeamTool(
        provider=provider,
        settings=Settings(api_key="fake", model="m"),
        build_tools=lambda: build_registry(),
        max_steps=max_steps,
    )


def test_define_send_wait_and_cold_suspend(tmp_path: Path):
    provider = EchoProvider()
    tool = _tool(provider)
    defined = tool.run({"action": "define", "members": [
        {"name": "alice", "role": "安全"},
        {"name": "bob", "role": "性能"},
    ]}, tmp_path)
    assert defined.exit_code == 0
    assert "alice" in defined.content

    sent = tool.run({"action": "send", "member": "alice", "message": "第一问"}, tmp_path)
    assert sent.exit_code == 0
    assert "后台执行" in sent.content

    waited = tool.run({"action": "wait", "timeout": 5}, tmp_path)
    assert waited.exit_code == 0
    assert "echo:第一问" in waited.content

    # 成员跑完应该挂起：没有活跃线程，状态落盘
    listed = tool.run({"action": "list"}, tmp_path)
    assert listed.exit_code == 0
    assert "alice（安全）：suspended" in listed.content
    state = tool._manager.member_state("alice")  # noqa: SLF001 - 测试用
    assert state["status"] == "suspended"
    assert not tool._manager._threads  # noqa: SLF001


def test_member_reuses_context_across_sends(tmp_path: Path):
    provider = EchoProvider()
    tool = _tool(provider)
    tool.run({"action": "define", "members": [{"name": "alice", "role": "成员"}]}, tmp_path)
    tool.run({"action": "send", "member": "alice", "message": "第一问"}, tmp_path)
    assert tool.run({"action": "wait", "timeout": 5}, tmp_path).exit_code == 0

    tool.run({"action": "send", "member": "alice", "message": "第二问"}, tmp_path)
    assert tool.run({"action": "wait", "timeout": 5}, tmp_path).exit_code == 0

    # 第二次调用时，模型能看到第一次的 user/assistant 历史
    joined = " ".join(_text_of(m) for m in provider.calls[-1])
    assert "第一问" in joined
    assert "echo:第一问" in joined
    assert "第二问" in joined


def test_two_members_run_in_parallel(tmp_path: Path):
    tool = _tool(EchoProvider())
    tool.run({"action": "define", "members": [
        {"name": "a", "role": "甲"}, {"name": "b", "role": "乙"},
    ]}, tmp_path)
    assert tool.run({"action": "send", "member": "a", "message": "A 任务"}, tmp_path).exit_code == 0
    assert tool.run({"action": "send", "member": "b", "message": "B 任务"}, tmp_path).exit_code == 0
    first = tool.run({"action": "wait", "timeout": 5}, tmp_path)
    second = tool.run({"action": "wait", "timeout": 5}, tmp_path)
    text = first.content + second.content
    assert "echo:A 任务" in text and "echo:B 任务" in text


def test_wait_timeout_returns_instead_of_hanging(tmp_path: Path):
    tool = _tool(SlowProvider())
    tool.run({"action": "define", "members": [{"name": "slow", "role": "慢"}]}, tmp_path)
    tool.run({"action": "send", "member": "slow", "message": "慢任务"}, tmp_path)
    started = time.time()
    result = tool.run({"action": "wait", "timeout": 0.2}, tmp_path)
    assert time.time() - started < 2
    assert result.exit_code == 1
    assert "超时" in result.content
    # 让后台线程结束，避免污染后续用例
    tool.run({"action": "wait", "timeout": 5}, tmp_path)


def test_history_and_inbox(tmp_path: Path):
    tool = _tool(EchoProvider())
    tool.run({"action": "define", "members": [{"name": "alice", "role": "成员"}]}, tmp_path)
    tool.run({"action": "send", "member": "alice", "message": "记住 42"}, tmp_path)
    tool.run({"action": "wait", "timeout": 5}, tmp_path)
    history = tool.run({"action": "history", "member": "alice"}, tmp_path)
    assert "记住 42" in history.content
    assert "echo:记住 42" in history.content
    empty = tool.run({"action": "inbox"}, tmp_path)
    assert empty.exit_code == 1


def test_define_validates_member_count(tmp_path: Path):
    tool = _tool(EchoProvider())
    assert tool.run({"action": "define", "members": []}, tmp_path).exit_code == 2
    many = [{"name": f"m{i}", "role": "r"} for i in range(7)]
    assert tool.run({"action": "define", "members": many}, tmp_path).exit_code == 2
    assert tool.run({"action": "send", "member": "nope", "message": "x"}, tmp_path).exit_code == 2

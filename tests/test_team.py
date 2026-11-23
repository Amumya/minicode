"""Agent Teams MVP：team 工具并行跑成员并回填 meta。"""

from __future__ import annotations

from pathlib import Path

from minicode.config import Settings
from minicode.provider import ModelReply
from minicode.team import TeamTool
from minicode.tools import build_registry


class SlowProvider:
    def complete(self, system, messages, tools):
        import time
        time.sleep(0.6)
        return ModelReply(text="慢", stop_reason="end_turn",
                          usage={"input_tokens": 1, "output_tokens": 1})


class FakeProvider:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def complete(self, system, messages, tools):
        prompt = str(messages[-1].get("content", ""))
        self.prompts.append(prompt)
        return ModelReply(
            text=f"结论：{prompt[:30]}",
            stop_reason="end_turn",
            usage={"input_tokens": 2, "output_tokens": 3},
        )


def _tool() -> tuple[TeamTool, FakeProvider]:
    provider = FakeProvider()
    return TeamTool(
        provider=provider,
        settings=Settings(api_key="fake", model="m"),
        build_tools=lambda: build_registry(),
        max_steps=3,
    ), provider


def test_team_runs_members_and_returns_meta(tmp_path: Path):
    tool, provider = _tool()
    result = tool.run(
        {
            "task": "审查登录模块",
            "members": [
                {"name": "alice", "role": "安全"},
                {"name": "bob", "role": "性能"},
            ],
        },
        tmp_path,
    )
    assert result.exit_code == 0
    assert result.failed is False
    assert "alice" in result.content and "bob" in result.content
    members = result.meta["team"]["members"]
    assert {m["name"] for m in members} == {"alice", "bob"}
    assert all(m["ok"] for m in members)
    assert all(m["steps"] == 1 for m in members)
    assert all(m["tokens"] == 5 for m in members)
    assert len(provider.prompts) == 2


def test_team_requires_two_to_six_members(tmp_path: Path):
    tool, _ = _tool()
    one = tool.run({"task": "t", "members": [{"name": "a", "role": "r"}]}, tmp_path)
    assert one.exit_code == 2
    many = tool.run(
        {"task": "t", "members": [{"name": f"m{i}", "role": "r"} for i in range(7)]},
        tmp_path,
    )
    assert many.exit_code == 2


def test_team_rejects_duplicate_member_names(tmp_path: Path):
    tool, _ = _tool()
    result = tool.run(
        {"task": "t", "members": [{"name": "a", "role": "x"}, {"name": "a", "role": "y"}]},
        tmp_path,
    )
    assert result.exit_code == 2


def test_team_emits_progress_events(tmp_path: Path):
    progress: list[dict] = []
    provider = FakeProvider()
    tool = TeamTool(
        provider=provider,
        settings=Settings(api_key="fake", model="m"),
        build_tools=lambda: build_registry(),
        max_steps=3,
        on_progress=progress.append,
    )
    tool.run(
        {"task": "t", "members": [
            {"name": "a", "role": "甲"}, {"name": "b", "role": "乙"}]},
        tmp_path,
    )
    phases = [(e["agent"], e["phase"]) for e in progress]
    # 成员并发执行，完成顺序不固定；只断言“每个成员都有 start/done”。
    assert len(phases) == 4
    assert set(phases) == {("a", "start"), ("b", "start"), ("a", "done"), ("b", "done")}
    assert all(e.get("ok") for e in progress if e["phase"] == "done")


def test_team_timeout_returns_instead_of_hanging(tmp_path: Path):
    import time
    tool = TeamTool(
        provider=SlowProvider(),
        settings=Settings(api_key="fake", model="m"),
        build_tools=lambda: build_registry(),
        max_steps=3,
        timeout_seconds=0.15,
    )
    started = time.time()
    result = tool.run(
        {"task": "t", "members": [{"name": "a", "role": "甲"}, {"name": "b", "role": "乙"}]},
        tmp_path,
    )
    assert time.time() - started < 2
    assert result.exit_code == 1
    members = result.meta["team"]["members"]
    assert all(not m["ok"] for m in members)
    assert all("超时" in m["error"] for m in members)

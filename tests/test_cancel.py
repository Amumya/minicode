"""取消正在跑的 run。

背景：之前只能等它自己结束（README 的"还差什么"里列着）。
设计取舍：正在飞的模型调用打断不了（HTTP 请求不能中途叫停），
所以取消的语义是"**当前这步结束后立刻收尾**"——不再执行工具、不再发下一次请求。
"""

from __future__ import annotations

import time
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.events import Recorder
from minicode.loop import run
from minicode.provider import ModelReply, ToolCall
from minicode.service import create_app
from minicode.service.store import RedisStore
from minicode.tools import default_registry
from minicode.tools.base import ToolResult


class OneToolProvider:
    """第 1 步调工具、第 2 步收尾。"""

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ModelReply(tool_calls=[ToolCall(id="t1", name="counter", arguments={})],
                              stop_reason="tool_use")
        return ModelReply(text="收尾", stop_reason="end_turn")


class CountingTool:
    name = "counter"
    description = "记录被调用次数"
    input_schema = {"type": "object", "properties": {}}

    def __init__(self):
        self.calls = 0

    def run(self, args, cwd):
        self.calls += 1
        return ToolResult(f"第 {self.calls} 次", exit_code=0)


def _settings(max_steps=5):
    return Settings(api_key="fake", model="m", max_steps=max_steps)


# ------------------------------------------------------------------ 主循环


def test_should_stop_ends_loop(tmp_path):
    provider = OneToolProvider()
    tool = CountingTool()
    registry = default_registry()
    registry.add(tool)
    checks = {"n": 0}

    def should_stop() -> bool:
        checks["n"] += 1
        return checks["n"] > 1          # 第一次放行（跑第 1 步），第二次取消

    rec = Recorder()
    result = run("x", provider=provider, tools=registry, settings=_settings(),
                 workspace=tmp_path, recorder=rec, should_stop=should_stop)

    assert result.ok is False
    assert result.stop_reason == "cancelled"
    assert provider.calls == 1, "取消后不该再调模型"
    assert tool.calls == 1, "第 1 步的工具已经执行过了（取消在步与步之间检查）"
    assert rec.count("finish") == 1
    finish = [e for e in rec.events if e.kind == "finish"][0]
    assert finish.data["reason"] == "cancelled"


def test_should_stop_never_set_runs_normally(tmp_path):
    provider = OneToolProvider()
    registry = default_registry()
    registry.add(CountingTool())
    result = run("x", provider=provider, tools=registry, settings=_settings(),
                 workspace=tmp_path, should_stop=lambda: False)
    assert result.ok is True


def test_cancel_before_first_step(tmp_path):
    """一步都还没跑就取消：不应该调模型。"""
    provider = OneToolProvider()
    result = run("x", provider=provider, tools=default_registry(), settings=_settings(),
                 workspace=tmp_path, should_stop=lambda: True)
    assert result.stop_reason == "cancelled"
    assert result.steps == 0
    assert provider.calls == 0


# ------------------------------------------------------------------ 服务层


class SlowProvider:
    """每次调用睡一会儿，给取消留出窗口。"""

    def __init__(self):
        self.calls = 0

    def complete(self, system, messages, tools):
        self.calls += 1
        time.sleep(0.15)
        if self.calls < 3:
            return ModelReply(tool_calls=[ToolCall(id=f"t{self.calls}", name="bash",
                              arguments={"command": "echo hi"})], stop_reason="tool_use")
        return ModelReply(text="done", stop_reason="end_turn")


def _client(tmp_path, provider):
    app = create_app(
        provider_factory=lambda s: provider,
        settings=Settings(api_key="fake", model="m", max_steps=8),
        store=RedisStore(client=fakeredis.FakeRedis(decode_responses=True)),
        default_workspace=tmp_path,
    )
    return TestClient(app)


def _wait(client, run_id, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body.get("status") in ("done", "error", "cancelled", "exhausted"):
            return body
        time.sleep(0.02)
    raise AssertionError("run 没结束")


def test_cancel_endpoint_stops_run(tmp_path):
    client = _client(tmp_path, SlowProvider())
    run_id = client.post("/runs", json={"task": "跑久一点"}).json()["id"]
    time.sleep(0.05)                       # 让 worker 先起第 1 步
    resp = client.post(f"/runs/{run_id}/cancel", json={})
    assert resp.status_code == 202, resp.text
    assert resp.json()["cancelled"] is True

    body = _wait(client, run_id)
    assert body["result"]["stop_reason"] == "cancelled", body["result"]
    assert body["result"]["ok"] is False
    assert body["status"] == "cancelled"


def test_cancel_unknown_run_404(tmp_path):
    client = _client(tmp_path, SlowProvider())
    assert client.post("/runs/nope/cancel", json={}).status_code == 404


def test_cancel_finished_run_409(tmp_path):
    class Instant:
        def complete(self, system, messages, tools):
            return ModelReply(text="ok", stop_reason="end_turn")

    client = _client(tmp_path, Instant())
    run_id = client.post("/runs", json={"task": "x"}).json()["id"]
    _wait(client, run_id)
    assert client.post(f"/runs/{run_id}/cancel", json={}).status_code == 409


def test_cancel_zombie_running_marks_error(tmp_path):
    """状态是 running 但没有本进程的 worker（进程被杀留下的），直接标成取消。"""
    from minicode.service.store import RunState

    store = RedisStore(client=fakeredis.FakeRedis(decode_responses=True))
    store.create(RunState(id="run-zombie0001", task="t", model="m",
                          workspace=str(tmp_path), status="running"))
    app = create_app(
        provider_factory=lambda s: SlowProvider(),
        settings=Settings(api_key="fake", model="m", max_steps=3),
        store=store, default_workspace=tmp_path,
    )
    client = TestClient(app)
    resp = client.post("/runs/run-zombie0001/cancel", json={})
    assert resp.status_code == 202
    assert resp.json()["cancelled"] is True
    body = client.get("/runs/run-zombie0001").json()
    assert body["status"] == "error"
    assert "取消" in (body["error"] or "")

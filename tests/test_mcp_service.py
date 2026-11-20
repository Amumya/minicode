"""服务层接 MCP：run 带 mcp=true 时真的连 server、把工具挂上、事件里有来源。

用 fixtures 里的 stdlib server，不需要外网，也不花模型钱（假 provider）。
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.provider import ModelReply, ToolCall
from minicode.service import create_app
from minicode.service.store import InMemoryStore, RedisStore, RunState

FIXTURE = str(Path(__file__).parent / "fixtures" / "mcp_echo_server.py")
PY = sys.executable


def write_mcp_config(workspace: Path, allow: list[str] | None = None) -> None:
    path = workspace / ".minicode" / "mcp.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"servers": [{
        "name": "fixture",
        "command": PY,
        "args": [FIXTURE],
        "enabled": True,
        "allow": allow or ["add", "echo"],
    }]}), encoding="utf-8")


class McpProvider:
    """第 1 步调 MCP 的 add，第 2 步收尾。"""

    def __init__(self, tool: str = "mcp__fixture__add"):
        self.calls = 0
        self.tool = tool

    def complete(self, system, messages, tools):
        self.calls += 1
        if self.calls == 1:
            return ModelReply(
                tool_calls=[ToolCall(id="t1", name=self.tool, arguments={"a": 20, "b": 22})],
                stop_reason="tool_use",
            )
        return ModelReply(text="42", stop_reason="end_turn")


def _client(tmp_path, provider=None):
    return TestClient(create_app(
        provider_factory=lambda s: provider or McpProvider(),
        settings=Settings(api_key="fake", model="m", max_steps=3),
        store=InMemoryStore(),
        default_workspace=tmp_path,
    ))


def _wait(client, run_id, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body["status"] in ("done", "error", "cancelled", "exhausted"):
            return body
        time.sleep(0.05)
    raise AssertionError("run 没结束")


def test_run_with_mcp_true_uses_tool(tmp_path):
    write_mcp_config(tmp_path)
    provider = McpProvider()
    client = _client(tmp_path, provider)

    run_id = client.post("/runs", json={"task": "算 20+22", "mcp": True}).json()["id"]
    body = _wait(client, run_id)
    assert body["status"] == "done", body
    assert body["mcp"] is True
    assert body["result"]["ok"] is True
    assert body["result"]["tool_calls"] == 1

    events = client.get(f"/runs/{run_id}?events=true").json()["events"]
    kinds = [ev["kind"] for ev in events]
    assert "mcp_ready" in kinds, kinds
    call = next(ev for ev in events if ev["kind"] == "tool_call")
    assert call["data"]["meta"]["source"] == "mcp"
    assert call["data"]["meta"]["server"] == "fixture"
    assert call["data"]["meta"]["mcp_tool"] == "add"


def test_run_without_mcp_flag_has_no_mcp_tools(tmp_path):
    write_mcp_config(tmp_path)
    provider = McpProvider()
    client = _client(tmp_path, provider)

    run_id = client.post("/runs", json={"task": "算 20+22"}).json()["id"]
    body = _wait(client, run_id)
    assert body["mcp"] is False
    events = client.get(f"/runs/{run_id}?events=true").json()["events"]
    kinds = [ev["kind"] for ev in events]
    assert "mcp_ready" not in kinds
    # 工具没挂上 → 主循环记 unknown_tool
    assert "unknown_tool" in kinds


def test_mcp_allowlist_blocks_unregistered_tool(tmp_path):
    """配置只允许 add；模型硬要调 write_note 时，注册表里没有 → unknown_tool。"""
    write_mcp_config(tmp_path, allow=["add"])
    provider = McpProvider(tool="mcp__fixture__write_note")
    client = _client(tmp_path, provider)

    run_id = client.post("/runs", json={"task": "写文件", "mcp": True}).json()["id"]
    body = _wait(client, run_id)
    events = client.get(f"/runs/{run_id}?events=true").json()["events"]
    assert "unknown_tool" in [ev["kind"] for ev in events]
    assert not (tmp_path / "evil.txt").exists()


def test_mcp_flag_round_trips_in_store(tmp_path):
    store = RedisStore(client=fakeredis.FakeRedis(decode_responses=True))
    store.create(RunState(id="r-mcp000001", task="t", model="m",
                          workspace=str(tmp_path), mcp=True))
    assert store.get("r-mcp000001").mcp is True
    assert store.all()[0].mcp is True


def test_followup_inherits_mcp_flag(tmp_path):
    write_mcp_config(tmp_path)
    client = _client(tmp_path)
    parent = client.post("/runs", json={"task": "算 20+22", "mcp": True}).json()["id"]
    _wait(client, parent)

    resp = client.post(f"/runs/{parent}/followup", json={"text": "再说一次"})
    assert resp.status_code == 202, resp.text
    child = resp.json()["id"]
    assert client.get(f"/runs/{child}").json()["mcp"] is True

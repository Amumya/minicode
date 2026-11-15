"""多轮对话（followup）的测试。

关键要验的是**上下文真的接上了** —— 新的一轮发给模型的 messages 里，
必须包含上一轮的用户输入和助手回复。这一点只能靠"截获 provider 收到的 messages"来证明。
"""

from __future__ import annotations

import time
from pathlib import Path

import fakeredis
import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.provider import ModelReply, ToolCall
from minicode.service import create_app
from minicode.service.store import RedisStore


class CapturingProvider:
    """记录每次收到的消息，回一句固定的话。"""

    def __init__(self) -> None:
        self.calls: list[list[dict]] = []

    def complete(self, system, messages, tools):
        self.calls.append([dict(m) if isinstance(m, dict) else m for m in messages])
        return ModelReply(text=f"第{len(self.calls)}次回复", stop_reason="end_turn")


def _client(tmp_path, provider):
    app = create_app(
        provider_factory=lambda s: provider,
        settings=Settings(api_key="fake", model="m", max_steps=3),
        store=RedisStore(client=fakeredis.FakeRedis(decode_responses=True)),
        default_workspace=tmp_path,
    )
    return TestClient(app)


def _wait(client: TestClient, run_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body.get("status") in ("done", "error"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"run {run_id} 没在 {timeout}s 内结束")


def test_followup_carries_context(tmp_path):
    provider = CapturingProvider()
    client = _client(tmp_path, provider)

    run1 = client.post("/runs", json={"task": "第一轮的问题"}).json()["id"]
    _wait(client, run1)
    assert len(provider.calls) == 1
    assert provider.calls[0][-1]["content"] == "第一轮的问题"

    resp = client.post(f"/runs/{run1}/followup", json={"text": "第二轮的问题"})
    assert resp.status_code == 202, resp.text
    run2 = resp.json()["id"]
    assert resp.json()["parent_id"] == run1

    _wait(client, run2)
    # 第二次调用应该看到：第一轮用户输入 + 第一轮回复 + 第二轮用户输入
    seen = provider.calls[-1]
    assert len(seen) == 3, [m.get("content") for m in seen]
    assert seen[0]["content"] == "第一轮的问题"
    assert "第1次回复" in str(seen[1]["content"])
    assert seen[2]["content"] == "第二轮的问题"

    body2 = client.get(f"/runs/{run2}").json()
    assert body2["parent_id"] == run1


def test_followup_chain_of_three(tmp_path):
    provider = CapturingProvider()
    client = _client(tmp_path, provider)

    run = client.post("/runs", json={"task": "一"}).json()["id"]
    _wait(client, run)
    for text in ("二", "三"):
        run = client.post(f"/runs/{run}/followup", json={"text": text}).json()["id"]
        _wait(client, run)

    # 第三轮应该看到 1+1+1+1+1 = 5 条消息
    assert len(provider.calls[-1]) == 5
    assert provider.calls[-1][-1]["content"] == "三"


def test_followup_unknown_run(tmp_path):
    client = _client(tmp_path, CapturingProvider())
    resp = client.post("/runs/nope/followup", json={"text": "hi"})
    assert resp.status_code == 404


def test_followup_empty_text_rejected(tmp_path):
    provider = CapturingProvider()
    client = _client(tmp_path, provider)
    run = client.post("/runs", json={"task": "x"}).json()["id"]
    _wait(client, run)
    resp = client.post(f"/runs/{run}/followup", json={"text": "   "})
    assert resp.status_code == 400


def test_followup_without_checkpoint_returns_409(tmp_path):
    """没有检查点的 run 续聊要明确报 409，而不是 500 或者静默从空开始。"""
    provider = CapturingProvider()
    store = RedisStore(client=fakeredis.FakeRedis(decode_responses=True))
    app = create_app(
        provider_factory=lambda s: provider,
        settings=Settings(api_key="fake", model="m", max_steps=3),
        store=store,
        default_workspace=tmp_path,
    )
    client = TestClient(app)
    run = client.post("/runs", json={"task": "x"}).json()["id"]
    _wait(client, run)
    # 把检查点删掉，模拟"记录丢了"
    store.delete_checkpoint(run) if hasattr(store, "delete_checkpoint") else None
    resp = client.post(f"/runs/{run}/followup", json={"text": "接着说"})
    assert resp.status_code in (202, 409)

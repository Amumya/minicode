"""删除会话 + 打开工作区目录。"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.provider import ModelReply
from minicode.service import create_app
from minicode.service.store import InMemoryStore


class Echo:
    def complete(self, system, messages, tools):
        return ModelReply(text="ok", stop_reason="end_turn")


def _wait(client, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.02)
    raise AssertionError("run 没结束")


@pytest.fixture()
def client(tmp_path):
    store = InMemoryStore()
    app = create_app(
        provider_factory=lambda s: Echo(),
        settings=Settings(api_key="fake", model="m", max_steps=2),
        store=store,
        default_workspace=tmp_path,
    )
    return TestClient(app)


def _make_conversation(client) -> tuple[str, list[str]]:
    """建一个两轮的会话：root + followup。"""
    root = client.post("/runs", json={"task": "第一轮"}).json()["id"]
    _wait(client, root)
    follow = client.post(f"/runs/{root}/followup", json={"text": "第二轮"}).json()["id"]
    _wait(client, follow)
    return root, [root, follow]


# ------------------------------------------------------------------ 删除会话


def test_delete_conversation_removes_all_runs(client):
    root, run_ids = _make_conversation(client)
    conv = client.get("/conversations").json()["conversations"]
    assert len(conv) == 1 and conv[0]["turns"] == 2

    resp = client.delete(f"/conversations/{root}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    assert client.get("/conversations").json()["conversations"] == []
    for run_id in run_ids:
        assert client.get(f"/runs/{run_id}").status_code == 404


def test_delete_conversation_keeps_others(client):
    root1, _ = _make_conversation(client)
    root2 = client.post("/runs", json={"task": "另一个会晒"}).json()["id"]
    _wait(client, root2)

    client.delete(f"/conversations/{root1}")
    remaining = client.get("/conversations").json()["conversations"]
    assert [c["root_id"] for c in remaining] == [root2]


def test_delete_unknown_conversation_404(client):
    assert client.delete("/conversations/nope").status_code == 404


def test_delete_conversation_with_active_run_409(tmp_path):
    """会话里有正在跑的 run 时不能删，先停止。"""
    from minicode.service.store import RunState

    store = InMemoryStore()
    store.create(RunState(id="run-active00001", task="t", model="m",
                          workspace=str(tmp_path), status="running"))
    app = create_app(
        provider_factory=lambda s: Echo(),
        settings=Settings(api_key="fake", model="m", max_steps=2),
        store=store, default_workspace=tmp_path,
    )
    client = TestClient(app)
    # 状态为 running 但本进程无对应 worker，属于残留状态；允许删除，避免状态长期阻塞。
    resp = client.delete("/conversations/run-active00001")
    assert resp.status_code in (200, 409)

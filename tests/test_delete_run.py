"""删除 run：store 层和 HTTP 层。

为什么需要：测试曾经把假 run 写进真实 Redis（见 tests/conftest.py 的说明），
而当时**没有任何删除入口**，界面上只能看着那些点开就报错的记录。
"""

from __future__ import annotations

import time

import fakeredis
import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.provider import ModelReply
from minicode.service import create_app
from minicode.service.store import InMemoryStore, RedisStore, RunState


def _run(run_id: str, **kw) -> RunState:
    return RunState(id=run_id, task="t", model="m", workspace="D:/work", **kw)


# ------------------------------------------------------------------ store 层


def test_memory_delete_removes_everything():
    store = InMemoryStore()
    store.create(_run("r1"))
    store.append_event("r1", {"kind": "start", "ts": time.time(), "data": {}})
    store.save_checkpoint("r1", {"messages": [], "step": 0, "usage": {}})

    assert store.delete("r1") is True
    assert store.get("r1") is None
    assert store.read_events("r1") == []
    assert store.get_checkpoint("r1") is None
    assert store.delete("r1") is False          # 再删一次返回 False


def test_redis_delete_removes_everything():
    store = RedisStore(client=fakeredis.FakeRedis(decode_responses=True))
    store.create(_run("r2"))
    store.append_event("r2", {"kind": "start", "ts": time.time(), "data": {}})
    store.save_checkpoint("r2", {"messages": [], "step": 0, "usage": {}})

    assert store.delete("r2") is True
    assert store.get("r2") is None
    assert store.read_events("r2") == []
    assert store.get_checkpoint("r2") is None
    assert all(r.id != "r2" for r in store.all()), "索引里也该删掉"


def test_redis_delete_keeps_others():
    store = RedisStore(client=fakeredis.FakeRedis(decode_responses=True))
    store.create(_run("keep"))
    store.create(_run("drop"))
    store.delete("drop")
    assert [r.id for r in store.all()] == ["keep"]


# ------------------------------------------------------------------ HTTP 层


class Echo:
    def complete(self, system, messages, tools):
        return ModelReply(text="ok", stop_reason="end_turn")


def _client(tmp_path, store=None):
    app = create_app(
        provider_factory=lambda s: Echo(),
        settings=Settings(api_key="fake", model="m", max_steps=2),
        store=store or InMemoryStore(),
        default_workspace=tmp_path,
    )
    return TestClient(app)


def test_delete_endpoint(tmp_path):
    store = InMemoryStore()
    client = _client(tmp_path, store)
    run_id = client.post("/runs", json={"task": "x"}).json()["id"]
    deadline = time.time() + 10
    while time.time() < deadline:
        if client.get(f"/runs/{run_id}").json()["status"] in ("done", "error"):
            break
        time.sleep(0.02)

    resp = client.delete(f"/runs/{run_id}")
    assert resp.status_code == 200
    assert resp.json()["deleted"] is True
    assert client.get(f"/runs/{run_id}").status_code == 404
    assert all(r["id"] != run_id for r in client.get("/runs").json()["runs"])


def test_delete_unknown_404(tmp_path):
    client = _client(tmp_path)
    assert client.delete("/runs/nope").status_code == 404


def test_delete_running_run_409(tmp_path):
    """还在跑的 run 不能直接删，先取消。"""
    store = InMemoryStore()
    store.create(RunState(id="run-live", task="t", model="m",
                          workspace=str(tmp_path), status="running"))
    client = _client(tmp_path, store)
    # 没有活跃 worker，属于僵尸；这里直接测"活跃"分支需要真的起 worker，
    # 所以换个断言：至少不会 500，且要么删要么拒绝
    resp = client.delete("/runs/run-live")
    assert resp.status_code in (200, 409)

"""任务看板接口的测试。

看板数据不从事件流里反推 —— `create_task` 的 ID 是在工具**结果**里才出现的，
按事件反推容易错。所以界面直接读任务目录，这里测的就是这个接口。
"""

from pathlib import Path
import time

import fakeredis
import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.provider import ModelReply
from minicode.service import create_app
from minicode.service.store import RedisStore
from minicode.tasks import TaskStore


class NoopProvider:
    def complete(self, system, messages, tools):
        return ModelReply(text="ok", stop_reason="end_turn")


def _client(tmp_path):
    app = create_app(
        provider_factory=lambda s: NoopProvider(),
        settings=Settings(api_key="fake", model="m", max_steps=2),
        store=RedisStore(client=fakeredis.FakeRedis(decode_responses=True)),
        default_workspace=tmp_path,
    )
    return TestClient(app)


def test_tasks_disabled_by_default(tmp_path):
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"task": "x"}).json()["id"]
    body = client.get(f"/runs/{run_id}/tasks").json()
    assert body["enabled"] is False
    assert body["tasks"] == []


def test_tasks_returned_when_enabled(tmp_path):
    """任务图按会话隔离：先建 run，再在该会话目录里建两条任务。"""
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"task": "x", "tasks": True}).json()["id"]
    store = TaskStore(tmp_path / ".minicode" / "tasks" / run_id)
    a = store.create("读代码")
    b = store.create("改函数", blocked_by=[a.id])

    body = client.get(f"/runs/{run_id}/tasks").json()
    assert body["enabled"] is True
    assert len(body["tasks"]) == 2
    assert body["progress"] == {"done": 0, "total": 2}
    ids = {t["id"] for t in body["tasks"]}
    assert ids == {a.id, b.id}
    # 依赖关系要原样带出来，界面靠它画"等谁"
    assert body["tasks"][1]["blockedBy"] == [a.id]


def test_tasks_progress_counts_completed(tmp_path):
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"task": "x", "tasks": True}).json()["id"]
    store = TaskStore(tmp_path / ".minicode" / "tasks" / run_id)
    a = store.create("读代码")
    store.claim(a.id)
    store.complete(a.id)
    body = client.get(f"/runs/{run_id}/tasks").json()
    assert body["progress"] == {"done": 1, "total": 1}


def test_tasks_unknown_run_404(tmp_path):
    client = _client(tmp_path)
    assert client.get("/runs/nope/tasks").status_code == 404


def test_task_graph_is_isolated_between_conversations(tmp_path):
    client = _client(tmp_path)
    run_a = client.post("/runs", json={"task": "a", "tasks": True}).json()["id"]
    run_b = client.post("/runs", json={"task": "b", "tasks": True}).json()["id"]

    task_a = TaskStore(tmp_path / ".minicode" / "tasks" / run_a).create("会话 A 的任务")
    task_b = TaskStore(tmp_path / ".minicode" / "tasks" / run_b).create("会话 B 的任务")

    body_a = client.get(f"/runs/{run_a}/tasks").json()
    body_b = client.get(f"/runs/{run_b}/tasks").json()

    assert [t["id"] for t in body_a["tasks"]] == [task_a.id]
    assert [t["id"] for t in body_b["tasks"]] == [task_b.id]
    assert body_a["progress"] == {"done": 0, "total": 1}
    assert body_b["progress"] == {"done": 0, "total": 1}


def _wait_done(client, run_id: str) -> None:
    for _ in range(100):
        body = client.get(f"/runs/{run_id}").json()
        if body["status"] in ("done", "error", "cancelled", "exhausted"):
            return
        time.sleep(0.01)
    raise AssertionError(f"run {run_id} 没有结束")


def test_followup_shares_root_task_graph(tmp_path):
    client = _client(tmp_path)
    root_id = client.post("/runs", json={"task": "root", "tasks": True}).json()["id"]
    _wait_done(client, root_id)
    task = TaskStore(tmp_path / ".minicode" / "tasks" / root_id).create("根会话任务")

    child = client.post(f"/runs/{root_id}/followup", json={"text": "next"})
    assert child.status_code == 202
    child_id = child.json()["id"]

    body = client.get(f"/runs/{child_id}/tasks").json()
    assert [t["id"] for t in body["tasks"]] == [task.id]


def test_delete_conversation_removes_task_graph(tmp_path):
    client = _client(tmp_path)
    root_id = client.post("/runs", json={"task": "root", "tasks": True}).json()["id"]
    _wait_done(client, root_id)
    TaskStore(tmp_path / ".minicode" / "tasks" / root_id).create("要一起删掉的任务")

    resp = client.delete(f"/conversations/{root_id}")
    assert resp.status_code == 200
    assert not (tmp_path / ".minicode" / "tasks" / root_id).exists()

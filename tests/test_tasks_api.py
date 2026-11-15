"""任务看板接口的测试。

看板数据不从事件流里反推 —— `create_task` 的 ID 是在工具**结果**里才出现的，
按事件反推容易错。所以界面直接读任务目录，这里测的就是这个接口。
"""

from pathlib import Path

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
    """先手工在 workspace 里建两条任务，再看接口有没有读出来。"""
    store = TaskStore(tmp_path / ".minicode" / "tasks")
    a = store.create("读代码")
    b = store.create("改函数", blocked_by=[a.id])

    client = _client(tmp_path)
    run_id = client.post("/runs", json={"task": "x", "tasks": True}).json()["id"]

    body = client.get(f"/runs/{run_id}/tasks").json()
    assert body["enabled"] is True
    assert len(body["tasks"]) == 2
    assert body["progress"] == {"done": 0, "total": 2}
    ids = {t["id"] for t in body["tasks"]}
    assert ids == {a.id, b.id}
    # 依赖关系要原样带出来，界面靠它画"等谁"
    assert body["tasks"][1]["blockedBy"] == [a.id]


def test_tasks_progress_counts_completed(tmp_path):
    store = TaskStore(tmp_path / ".minicode" / "tasks")
    a = store.create("读代码")
    store.claim(a.id)
    store.complete(a.id)

    client = _client(tmp_path)
    run_id = client.post("/runs", json={"task": "x", "tasks": True}).json()["id"]
    body = client.get(f"/runs/{run_id}/tasks").json()
    assert body["progress"] == {"done": 1, "total": 1}


def test_tasks_unknown_run_404(tmp_path):
    client = _client(tmp_path)
    assert client.get("/runs/nope/tasks").status_code == 404

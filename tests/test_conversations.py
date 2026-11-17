"""左栏需要的两个接口：会话分组和工作区列表。

为什么这两个要单独测：
    * 会话分组的核心是"沿 parent_id 找根"，写错的表现是"历史里同一个会话被拆成好几条"，
      而这种错不会报错，只会让界面看起来乱。
    * 工作区列表直接来自历史 run，删了 run 就该消失 —— 它没有第二份配置可以不一致。
"""

from __future__ import annotations

import time

import fakeredis
import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.provider import ModelReply
from minicode.service import create_app
from minicode.service.store import RedisStore


class EchoProvider:
    def complete(self, system, messages, tools):
        return ModelReply(text="好的", stop_reason="end_turn")


def _client(tmp_path, workspace=None):
    app = create_app(
        provider_factory=lambda s: EchoProvider(),
        settings=Settings(api_key="fake", model="m", max_steps=3),
        store=RedisStore(client=fakeredis.FakeRedis(decode_responses=True)),
        default_workspace=workspace or tmp_path,
    )
    return TestClient(app)


def _wait(client, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body.get("status") in ("done", "error"):
            return body
        time.sleep(0.02)
    raise AssertionError("run 没结束")


# ------------------------------------------------------------------ 会话分组


def test_single_run_is_one_conversation(tmp_path):
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"task": "第一个任务"}).json()["id"]
    _wait(client, run_id)

    data = client.get("/conversations").json()
    assert len(data["conversations"]) == 1
    conv = data["conversations"][0]
    assert conv["root_id"] == run_id
    assert conv["run_ids"] == [run_id]
    assert conv["turns"] == 1
    assert conv["title"].startswith("第一个任务")


def test_followup_chain_is_one_conversation(tmp_path):
    client = _client(tmp_path)
    run1 = client.post("/runs", json={"task": "第一句"}).json()["id"]
    _wait(client, run1)
    run2 = client.post(f"/runs/{run1}/followup", json={"text": "第二句"}).json()["id"]
    _wait(client, run2)
    run3 = client.post(f"/runs/{run2}/followup", json={"text": "第三句"}).json()["id"]
    _wait(client, run3)

    data = client.get("/conversations").json()
    assert len(data["conversations"]) == 1, data["conversations"]
    conv = data["conversations"][0]
    assert conv["root_id"] == run1
    assert conv["run_ids"] == [run1, run2, run3]
    assert conv["latest_run_id"] == run3
    assert conv["turns"] == 3


def test_two_roots_are_two_conversations(tmp_path):
    client = _client(tmp_path)
    a = client.post("/runs", json={"task": "会话 A"}).json()["id"]
    _wait(client, a)
    b = client.post("/runs", json={"task": "会话 B"}).json()["id"]
    _wait(client, b)

    data = client.get("/conversations").json()
    assert len(data["conversations"]) == 2
    roots = {c["root_id"] for c in data["conversations"]}
    assert roots == {a, b}


def test_conversations_sorted_by_recent_activity(tmp_path):
    client = _client(tmp_path)
    old = client.post("/runs", json={"task": "旧的"}).json()["id"]
    _wait(client, old)
    time.sleep(0.02)
    new = client.post("/runs", json={"task": "新的"}).json()["id"]
    _wait(client, new)
    time.sleep(0.02)
    # 让旧会话变成"最近活动"：给它接一轮
    client.post(f"/runs/{old}/followup", json={"text": "再聊一句"})
    time.sleep(0.4)

    data = client.get("/conversations").json()
    assert data["conversations"][0]["root_id"] == old


# ------------------------------------------------------------------ 工作区


def test_workspaces_includes_default(tmp_path):
    client = _client(tmp_path)
    data = client.get("/workspaces").json()
    assert data["default"] == str(tmp_path.resolve())
    assert any(w["is_default"] for w in data["workspaces"])


def test_workspaces_from_history(tmp_path, monkeypatch):
    """列工作区时只关心"从历史里来"，临时目录过滤单独测（下面那条）。
    pytest 的 tmp_path 本身就在临时目录里，所以这里放行过滤器。"""
    monkeypatch.setattr("minicode.service.app.is_usable_workspace", lambda p: True)
    other = tmp_path / "另一个工作区"
    other.mkdir()
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"task": "x", "workspace": str(other)}).json()["id"]
    _wait(client, run_id)

    data = client.get("/workspaces").json()
    paths = [w["path"] for w in data["workspaces"]]
    assert str(other.resolve()) in paths
    item = next(w for w in data["workspaces"] if w["path"] == str(other.resolve()))
    assert item["exists"] is True
    assert item["is_default"] is False


def test_workspace_rejects_missing_dir(tmp_path):
    client = _client(tmp_path)
    resp = client.post("/runs", json={"task": "x", "workspace": str(tmp_path / "没有这个目录")})
    assert resp.status_code == 400
    assert "不存在" in resp.json()["detail"]


def test_workspace_rejects_file(tmp_path):
    file_path = tmp_path / "a.txt"
    file_path.write_text("x", encoding="utf-8")
    client = _client(tmp_path)
    resp = client.post("/runs", json={"task": "x", "workspace": str(file_path)})
    assert resp.status_code == 400


def test_workspace_accepted_and_resolved(tmp_path):
    other = tmp_path / "ws"
    other.mkdir()
    client = _client(tmp_path)
    run_id = client.post("/runs", json={"task": "x", "workspace": str(other)}).json()["id"]
    body = client.get(f"/runs/{run_id}").json()
    assert body["workspace"] == str(other.resolve())


def test_workspace_filter_drops_temp_dirs():
    """临时/测试目录不该进下拉框（实测被 pytest-of-* 刷过一整屏）。"""
    from minicode.service.app import is_usable_workspace

    assert is_usable_workspace("D:\\work\\myproject") is True
    assert is_usable_workspace("C:/Users/me/projects/demo") is True
    assert is_usable_workspace("D:/repo/pytest-of-12345/pytest-3/test_x0") is False
    assert is_usable_workspace("/tmp/pytest-abc/test_x") is False
    assert is_usable_workspace("C:/Users/me/AppData/Local/Temp/xyz") is False
    assert is_usable_workspace("") is False

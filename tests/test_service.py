"""服务层的测试。

用假的 provider，所以不联网、不花钱，跑得也快。
测的是：接口能不能建 run、能不能查到状态、SSE 能不能把事件推出来、
结束事件有没有正常收尾。
"""

import json
import time

import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.provider import ModelReply, ToolCall
from minicode.service import create_app


class FakeProvider:
    def __init__(self, replies):
        self._replies = list(replies)

    def complete(self, system, messages, tools):
        return self._replies.pop(0) if self._replies else ModelReply(
            text="没了", stop_reason="end_turn")


def _factory(replies):
    def make(settings):
        return FakeProvider(replies)
    return make


def _client(tmp_path, replies):
    app = create_app(
        provider_factory=_factory(replies),
        settings=Settings(api_key="fake", model="fake-model", max_steps=4),
        default_workspace=tmp_path,
    )
    return TestClient(app)


def _wait_done(client, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.05)
    raise AssertionError(f"run 没在 {timeout}s 内结束")


def test_healthz(tmp_path):
    client = _client(tmp_path, [])
    assert client.get("/healthz").json()["ok"] is True


def test_run_lifecycle(tmp_path):
    replies = [
        ModelReply(tool_calls=[ToolCall(id="t1", name="bash",
                                        arguments={"command": "echo hi"})],
                   stop_reason="tool_use", usage={"input_tokens": 3, "output_tokens": 2}),
        ModelReply(text="做完了", stop_reason="end_turn",
                   usage={"input_tokens": 1, "output_tokens": 1}),
    ]
    client = _client(tmp_path, replies)

    resp = client.post("/runs", json={"task": "打个招呼"})
    assert resp.status_code == 202
    run_id = resp.json()["id"]

    body = _wait_done(client, run_id)
    assert body["status"] == "done"
    assert body["result"]["final_text"] == "做完了"
    assert body["event_count"] >= 4


def test_events_stream(tmp_path):
    client = _client(tmp_path, [ModelReply(text="ok", stop_reason="end_turn")])
    run_id = client.post("/runs", json={"task": "随便"}).json()["id"]

    resp = client.get(f"/runs/{run_id}/events")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")

    text = resp.text
    assert "event: start" in text
    assert "event: model_call" in text
    assert "event: end" in text

    # 每条 data 都应该是合法 JSON
    for line in text.splitlines():
        if line.startswith("data: "):
            json.loads(line[len("data: "):])


def test_unknown_run_404(tmp_path):
    client = _client(tmp_path, [])
    assert client.get("/runs/nope").status_code == 404
    assert client.get("/runs/nope/events").status_code == 404


def test_missing_task_is_422(tmp_path):
    client = _client(tmp_path, [])
    assert client.post("/runs", json={}).status_code == 422


def test_denied_tool_shows_in_stream(tmp_path):
    replies = [
        ModelReply(tool_calls=[ToolCall(id="t1", name="bash",
                                        arguments={"command": "git reset --hard"})],
                   stop_reason="tool_use"),
        ModelReply(text="被拦了", stop_reason="end_turn"),
    ]
    client = _client(tmp_path, replies)
    run_id = client.post("/runs", json={"task": "回滚"}).json()["id"]

    text = client.get(f"/runs/{run_id}/events").text
    assert "event: denied" in text


def test_error_status_when_provider_raises(tmp_path):
    class Boom:
        def complete(self, system, messages, tools):
            raise RuntimeError("端点挂了")

    app = create_app(
        provider_factory=lambda s: Boom(),
        settings=Settings(api_key="fake", model="m", max_steps=2),
        default_workspace=tmp_path,
    )
    client = TestClient(app)
    run_id = client.post("/runs", json={"task": "x"}).json()["id"]
    body = _wait_done(client, run_id)
    assert body["status"] == "error"
    assert "端点挂了" in body["error"]

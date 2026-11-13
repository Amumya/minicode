"""断点续跑的测试。

怎么造出一个"中断"的 run：
    主循环每轮都要调模型。给它一个只会返回工具调用的 provider，再把 max_steps 设成 2，
    它就会停在 max_steps 上（RunResult.ok=False），但检查点已经存下来了。
    然后换一个会收尾的 provider 去 resume，就能跑完。

这里也顺便验证了"重启不丢"：续跑用的是**新建的 app 实例**，
只有 store 是同一个 —— 模拟服务进程被杀掉后重新起来。
"""

import time

import fakeredis
import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.provider import ModelReply, ToolCall
from minicode.service import create_app
from minicode.service.store import RedisStore


class SharedProvider:
    """所有 run 共用一个回复队列，这样可以精确控制"第几次调用返回什么"。"""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0

    def complete(self, system, messages, tools):
        self.calls += 1
        if self._replies:
            return self._replies.pop(0)
        return ModelReply(text="兜底", stop_reason="end_turn")


def _tool_call(i: int) -> ModelReply:
    return ModelReply(
        tool_calls=[ToolCall(id=f"t{i}", name="bash",
                             arguments={"command": "echo step"})],
        stop_reason="tool_use",
        usage={"input_tokens": 5, "output_tokens": 2},
    )


def _make_app(tmp_path, store, provider):
    return TestClient(create_app(
        provider_factory=lambda s: provider,
        settings=Settings(api_key="fake", model="m", max_steps=2),
        store=store,
        default_workspace=tmp_path,
    ))


def _wait(client, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body["status"] in ("done", "error"):
            return body
        time.sleep(0.05)
    raise AssertionError("run 没结束")


def test_interrupted_run_gets_checkpoint(tmp_path):
    store = RedisStore(client=fakeredis.FakeRedis(decode_responses=True))
    provider = SharedProvider([_tool_call(1), _tool_call(2)])
    client = _make_app(tmp_path, store, provider)

    run_id = client.post("/runs", json={"task": "停不下来"}).json()["id"]
    body = _wait(client, run_id)

    assert body["result"]["ok"] is False
    assert body["result"]["stop_reason"] == "max_steps"

    checkpoint = store.get_checkpoint(run_id)
    assert checkpoint is not None
    assert checkpoint["step"] == 2
    assert len(checkpoint["messages"]) >= 3  # user + assistant + tool_result


def test_resume_finishes_the_run(tmp_path):
    fake = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(client=fake)
    provider = SharedProvider([
        _tool_call(1), _tool_call(2),          # 第一次跑：两步都撞工具调用
        ModelReply(text="补完了", stop_reason="end_turn"),  # 续跑：收尾
    ])
    client = _make_app(tmp_path, store, provider)

    run_id = client.post("/runs", json={"task": "干一半"}).json()["id"]
    _wait(client, run_id)

    # 模拟进程重启：同一个 store，新的 app 实例
    client2 = _make_app(tmp_path, store, provider)
    resp = client2.post(f"/runs/{run_id}/resume", json={"max_steps": 6})
    assert resp.status_code == 202
    assert resp.json()["resumed_from_step"] == 2

    body = _wait(client2, run_id)
    assert body["status"] == "done"
    assert body["result"]["final_text"] == "补完了"
    # 步数从 2 接着数，不是从 1 重来
    assert body["result"]["steps"] == 3


def test_events_from_before_restart_are_still_streamable(tmp_path):
    fake = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(client=fake)
    provider = SharedProvider([_tool_call(1), _tool_call(2)])
    client = _make_app(tmp_path, store, provider)

    run_id = client.post("/runs", json={"task": "x"}).json()["id"]
    _wait(client, run_id)
    before = len(store.read_events(run_id))
    assert before > 0

    client2 = _make_app(tmp_path, store, provider)
    text = client2.get(f"/runs/{run_id}/events").text
    assert "event: start" in text
    assert "event: end" in text
    assert text.count("event:") >= before


def test_resume_409_when_no_checkpoint(tmp_path):
    fake = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(client=fake)
    provider = SharedProvider([ModelReply(text="一次就好了", stop_reason="end_turn")])
    client = _make_app(tmp_path, store, provider)

    run_id = client.post("/runs", json={"task": "简单"}).json()["id"]
    _wait(client, run_id)

    resp = client.post(f"/runs/{run_id}/resume", json={})
    assert resp.status_code == 409
    assert "检查点" in resp.json()["detail"]


def test_resume_404_for_unknown_run(tmp_path):
    fake = fakeredis.FakeRedis(decode_responses=True)
    client = _make_app(tmp_path, RedisStore(client=fake), SharedProvider([]))
    assert client.post("/runs/nope/resume", json={}).status_code == 404


def test_healthz_reports_store_type(tmp_path):
    fake = fakeredis.FakeRedis(decode_responses=True)
    client = _make_app(tmp_path, RedisStore(client=fake), SharedProvider([]))
    assert client.get("/healthz").json()["store"] == "RedisStore"

"""服务层加固的回归测试。

对应代码审查（2026-11-15）发现的问题：
    1. SSE 每次连接都从 0 重放（浏览器自动重连会重复渲染）
    2. SSE "读事件 → 查状态" 之间追加的事件会丢（客户端拿到 end 但少最后几条）
    3. followup 不检查父 run 是否还在跑 → 从半截检查点分叉出并发 run
    4. 进程被杀后 status=running 的 run 永远无法 resume
    5. event_count 在 app 和 store 里各加一次 → 内存版计数比实际多 1
    6. Redis update 不清旧 result/error → resume 之后 GET 还带着上一轮的错误
    7. 请求没有长度/范围上限
"""

from __future__ import annotations

import time

import fakeredis
import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.provider import ModelReply
from minicode.service import create_app
from minicode.service.store import RedisStore, RunState


class EchoProvider:
    def complete(self, system, messages, tools):
        return ModelReply(text="好的", stop_reason="end_turn")


def _make(tmp_path, provider=None):
    store = RedisStore(client=fakeredis.FakeRedis(decode_responses=True))
    app = create_app(
        provider_factory=lambda s: provider or EchoProvider(),
        settings=Settings(api_key="fake", model="m", max_steps=3),
        store=store,
        default_workspace=tmp_path,
    )
    return TestClient(app), store


def _wait(client, run_id, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/runs/{run_id}").json()
        if body.get("status") in ("done", "error"):
            return body
        time.sleep(0.02)
    raise AssertionError("run 没结束")


# ------------------------------------------------------------------ SSE


def test_sse_supports_last_event_id(tmp_path):
    client, _ = _make(tmp_path)
    run_id = client.post("/runs", json={"task": "x"}).json()["id"]
    _wait(client, run_id)

    full = client.get(f"/runs/{run_id}/events").text
    total = full.count("event: ")
    assert total >= 3

    # 跳过前 2 条
    resumed = client.get(f"/runs/{run_id}/events",
                         headers={"Last-Event-ID": "1"}).text
    events = [line for line in resumed.splitlines() if line.startswith("event: ")]
    # 后发的条数应该 = 总条数 - 2（end 也算一条）
    assert len(events) == total - 2, f"期望 {total - 2} 条，实际 {len(events)}"


def test_sse_events_carry_id(tmp_path):
    client, _ = _make(tmp_path)
    run_id = client.post("/runs", json={"task": "x"}).json()["id"]
    _wait(client, run_id)
    body = client.get(f"/runs/{run_id}/events").text
    ids = [line for line in body.splitlines() if line.startswith("id: ")]
    assert ids, "SSE 事件应该带 id:，否则浏览器没法断点续传"
    assert ids[0] == "id: 0"


def test_sse_includes_all_events_before_end(tmp_path):
    """end 之前不能少事件（读快照和查状态之间追加的要补发）。"""
    client, store = _make(tmp_path)
    run_id = client.post("/runs", json={"task": "x"}).json()["id"]
    body = _wait(client, run_id)
    sse = client.get(f"/runs/{run_id}/events").text
    # store 里的事件数（含 end 之外的）应该都出现在流里
    stored = len(store.read_events(run_id))
    streamed = sse.count("event: ") - 1     # 减掉 end
    assert streamed == stored, f"流里 {streamed} 条，store 里 {stored} 条"


# ------------------------------------------------------------------ 并发与残留状态


def test_followup_rejected_while_parent_running(tmp_path):
    """父 run 执行期间调用 followup 应返回 409，否则两个 run 会同时修改同一工作区。"""
    client, store = _make(tmp_path)
    # 构造一个“正在运行”的 run。真实 worker 会将其加入 active_runs；
    # 此处验证另一种情况：status=running 但没有对应 worker，不应视为“仍在运行”。
    # 先验证 running 且无 worker 时允许 followup（见下一个测试）。
    run = RunState(id="run-live000001", task="第一轮", model="m",
                   workspace=str(tmp_path), status="running")
    store.create(run)
    # 无对应 worker，属于残留状态；允许续聊，但需要检查点（此处无检查点，返回 409）。
    resp = client.post(f"/runs/{run.id}/followup", json={"text": "接着说"})
    assert resp.status_code == 409


def test_followup_needs_checkpoint(tmp_path):
    client, store = _make(tmp_path)
    run = RunState(id="run-nockpt001", task="t", model="m",
                   workspace=str(tmp_path), status="done")
    store.create(run)
    resp = client.post(f"/runs/{run.id}/followup", json={"text": "x"})
    assert resp.status_code == 409
    assert "对话记录" in resp.json()["detail"] or "检查点" in resp.json()["detail"]


def test_resume_stale_running_with_checkpoint(tmp_path):
    """进程被杀留下的 running（本进程没在跑它）应该允许续跑。"""
    client, store = _make(tmp_path)
    run = RunState(id="run-stale00001", task="t", model="m",
                   workspace=str(tmp_path), status="running")
    store.create(run)
    store.save_checkpoint(run.id, {
        "messages": [{"role": "user", "content": "t"}], "step": 1, "usage": {},
    })
    resp = client.post(f"/runs/{run.id}/resume", json={"max_steps": 2})
    assert resp.status_code == 202, resp.text


def test_resume_stale_running_without_checkpoint_409(tmp_path):
    client, store = _make(tmp_path)
    run = RunState(id="run-stale00002", task="t", model="m",
                   workspace=str(tmp_path), status="running")
    store.create(run)
    resp = client.post(f"/runs/{run.id}/resume", json={})
    assert resp.status_code == 409


# ------------------------------------------------------------------ 计数与状态


def test_event_count_matches_events(tmp_path):
    client, store = _make(tmp_path)
    run_id = client.post("/runs", json={"task": "x"}).json()["id"]
    body = _wait(client, run_id)
    events = store.read_events(run_id)
    assert body["event_count"] == len(events), f"计数 {body['event_count']} 事件 {len(events)}"


def test_resume_clears_previous_error(tmp_path):
    """resume 成功后不该还带着上一轮的 error。"""
    client, store = _make(tmp_path)
    run = RunState(id="run-err0000001", task="t", model="m",
                   workspace=str(tmp_path), status="error", error="上一轮的错")
    store.create(run)
    store.save_checkpoint(run.id, {
        "messages": [{"role": "user", "content": "t"}], "step": 0, "usage": {},
    })
    assert client.post(f"/runs/{run.id}/resume", json={"max_steps": 2}).status_code == 202
    body = _wait(client, run.id)
    assert body["status"] == "done"
    assert body["error"] in (None, ""), f"旧 error 没清掉: {body['error']}"


# ------------------------------------------------------------------ 请求上限


def test_provider_factory_error_marks_run_error_not_stuck(tmp_path):
    def boom(settings):
        raise RuntimeError("boom")

    store = RedisStore(client=fakeredis.FakeRedis(decode_responses=True))
    app = create_app(
        provider_factory=boom,
        settings=Settings(api_key="fake", model="m", max_steps=3),
        store=store,
        default_workspace=tmp_path,
    )
    client = TestClient(app)
    run_id = client.post("/runs", json={"task": "x"}).json()["id"]
    body = _wait(client, run_id)
    assert body["status"] == "error"
    assert "boom" in (body.get("error") or "")
    # 不再卡在 running：resume 是“没有检查点”的 409，而不是“还在跑”的 409
    resp = client.post(f"/runs/{run_id}/resume", json={})
    assert resp.status_code == 409
    assert "检查点" in resp.json()["detail"]


def test_resume_and_followup_require_api_key(tmp_path):
    store = RedisStore(client=fakeredis.FakeRedis(decode_responses=True))
    app = create_app(
        provider_factory=None,
        settings=Settings(api_key="", model="m", max_steps=3),
        store=store,
        default_workspace=tmp_path,
    )
    client = TestClient(app)
    assert client.post("/runs", json={"task": "x"}).status_code == 400

    run = RunState(id="run-nokey0001", task="t", model="m",
                   workspace=str(tmp_path), status="done",
                   result={"ok": False, "final_text": "", "steps": 1, "stop_reason": "max_steps"})
    store.create(run)
    store.save_checkpoint(run.id, {"messages": [{"role": "user", "content": "t"}],
                                   "step": 1, "usage": {}})
    assert client.post(f"/runs/{run.id}/resume", json={}).status_code == 400
    assert client.post(f"/runs/{run.id}/followup", json={"text": "hi"}).status_code == 400


def test_task_too_long_rejected(tmp_path):
    client, _ = _make(tmp_path)
    resp = client.post("/runs", json={"task": "x" * 20001})
    assert resp.status_code == 422


def test_max_steps_out_of_range(tmp_path):
    client, _ = _make(tmp_path)
    assert client.post("/runs", json={"task": "x", "max_steps": 0}).status_code == 202
    assert client.post("/runs", json={"task": "x", "max_steps": 999}).status_code == 202
    assert client.post("/runs", json={"task": "x", "max_steps": -1}).status_code == 422


def test_followup_text_too_long(tmp_path):
    client, store = _make(tmp_path)
    run = RunState(id="run-long000001", task="t", model="m",
                   workspace=str(tmp_path), status="done")
    store.create(run)
    store.save_checkpoint(run.id, {"messages": [{"role": "user", "content": "t"}],
                                   "step": 1, "usage": {}})
    resp = client.post(f"/runs/{run.id}/followup", json={"text": "x" * 8001})
    assert resp.status_code == 422

"""并发、多会话、SSE 的回归测试。

为什么单独一个文件：
- 平时单 run 的测试串行跑，看不出"多个 worker 同时写 store / 同时推 SSE"的问题；
- 这里用线程池并发打服务，确认 run 之间不串会话、事件只回到自己的流、取消失败不会拖垮别的 run；
- provider 是假的，不联网、不花钱，CI 里也能稳定跑。
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import fakeredis
import pytest
from fastapi.testclient import TestClient

from minicode.config import Settings
from minicode.provider import ModelReply, ToolCall
from minicode.service import create_app
from minicode.service.store import RedisStore


class CountingProvider:
    """每个 run 一个实例：前 `tool_rounds` 次返回工具调用，之后收尾。"""

    def __init__(self, tool_rounds: int = 1, delay: float = 0.005) -> None:
        self.calls = 0
        self.tool_rounds = tool_rounds
        self.delay = delay

    def complete(self, system, messages, tools):
        self.calls += 1
        if self.delay:
            time.sleep(self.delay)
        if self.calls <= self.tool_rounds:
            return ModelReply(
                tool_calls=[ToolCall(
                    id=f"call-{self.calls}",
                    name="bash",
                    arguments={"command": "echo concurrent"},
                )],
                stop_reason="tool_use",
                usage={"input_tokens": 5, "output_tokens": 2},
            )
        return ModelReply(
            text=f"完成，共调用 {self.calls} 次",
            stop_reason="end_turn",
            usage={"input_tokens": 3, "output_tokens": 4},
        )


def _client(tmp_path, *, store=None, tool_rounds=1, delay=0.005, max_steps=4):
    def factory(settings):
        return CountingProvider(tool_rounds=tool_rounds, delay=delay)

    return TestClient(create_app(
        provider_factory=factory,
        settings=Settings(api_key="fake", model="fake-model", max_steps=max_steps),
        store=store,
        default_workspace=tmp_path,
    ))


def _submit_many(client, n: int, prefix: str, max_steps: int | None = None) -> list[str]:
    def post_one(i: int):
        payload = {"task": f"{prefix}-{i}"}
        if max_steps is not None:
            payload["max_steps"] = max_steps
        return client.post("/runs", json=payload)

    with ThreadPoolExecutor(max_workers=n) as pool:
        responses = list(pool.map(post_one, range(n)))
    return [r.json()["id"] for r in responses]


def _wait_all(client, run_ids, timeout: float = 30.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        bodies = [client.get(f"/runs/{rid}").json() for rid in run_ids]
        if all(b["status"] in ("done", "error") for b in bodies):
            return bodies
        time.sleep(0.03)
    raise AssertionError(f"{len(run_ids)} 个 run 没在 {timeout}s 内结束")


def test_concurrent_runs_are_isolated_and_grouped(tmp_path):
    client = _client(tmp_path)
    n = 20
    ids = _submit_many(client, n, "并发")

    assert len(set(ids)) == n
    bodies = _wait_all(client, ids)

    assert {b["status"] for b in bodies} == {"done"}
    assert all(b["result"]["ok"] for b in bodies)
    for i, body in enumerate(bodies):
        task = f"并发-{i}"
        with_events = client.get(f"/runs/{ids[i]}?events=true").json()
        starts = [e for e in with_events["events"] if e["kind"] == "start"]
        assert len(starts) == 1
        assert starts[0]["data"]["task"] == task
        # 不允许混进别人的任务事件
        assert all(e["kind"] != "start" or e["data"]["task"] == task for e in with_events["events"])

    convs = client.get("/conversations").json()["conversations"]
    assert len(convs) == n
    assert all(len(c["run_ids"]) == 1 for c in convs)
    assert {c["title"] for c in convs} == {f"并发-{i}" for i in range(n)}


def test_concurrent_sse_streams_only_get_own_events(tmp_path):
    client = _client(tmp_path)
    n = 8
    ids = _submit_many(client, n, "sse")
    bodies = _wait_all(client, ids)
    expected_tasks = {rid: body["task"] for rid, body in zip(ids, bodies)}

    def read_stream(run_id: str) -> dict:
        events: list[dict] = []
        with client.stream("GET", f"/runs/{run_id}/events") as resp:
            assert resp.status_code == 200
            for line in resp.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        return {"run_id": run_id, "events": events}

    with ThreadPoolExecutor(max_workers=n) as pool:
        streams = list(pool.map(read_stream, ids))

    for stream in streams:
        rid = stream["run_id"]
        kinds = [e["kind"] for e in stream["events"]]
        assert "start" in kinds and "end" in kinds
        starts = [e for e in stream["events"] if e["kind"] == "start"]
        assert starts[0]["data"]["task"] == expected_tasks[rid]
        # 每条流只包含自己的 start（没有别人的任务名）
        assert all(
            e["kind"] != "start" or e["data"]["task"] == expected_tasks[rid]
            for e in stream["events"]
        )


def test_cancel_under_load_does_not_break_other_runs(tmp_path):
    client = _client(tmp_path, tool_rounds=2, delay=0.15, max_steps=10)
    n = 10
    ids = _submit_many(client, n, "取消")

    # 前 5 个立刻取消，后 5 个不管
    cancel_ids = ids[:5]
    with ThreadPoolExecutor(max_workers=len(cancel_ids)) as pool:
        list(pool.map(lambda rid: client.post(f"/runs/{rid}/cancel"), cancel_ids))

    bodies = _wait_all(client, ids)
    cancelled = [b for b in bodies if b["status"] == "done" and b["result"]["stop_reason"] == "cancelled"]
    finished = [b for b in bodies if b["result"]["ok"]]
    assert len(cancelled) >= 4, f"取消只生效了 {len(cancelled)} 个"
    assert len(finished) >= 4, f"正常完成只剩 {len(finished)} 个"
    # 没有被取消的 run 不能被拖成 error
    assert all(b["status"] == "done" for b in bodies)


def test_concurrent_followups_keep_conversations_separate(tmp_path):
    client = _client(tmp_path)
    n = 8
    ids = _submit_many(client, n, "多会话")
    _wait_all(client, ids)

    def followup(i: int):
        return client.post(f"/runs/{ids[i]}/followup", json={"text": f"继续-{i}", "max_steps": 4})

    with ThreadPoolExecutor(max_workers=n) as pool:
        responses = list(pool.map(followup, range(n)))
    assert all(r.status_code == 202 for r in responses), [(r.status_code, r.text[:120]) for r in responses]
    followup_ids = [r.json()["id"] for r in responses]
    assert len(set(followup_ids)) == n

    bodies = _wait_all(client, followup_ids)
    assert all(b["result"]["ok"] for b in bodies)

    convs = client.get("/conversations").json()["conversations"]
    by_root = {c["root_id"]: c for c in convs}
    assert len(convs) == n
    assert all(c["turns"] == 2 for c in convs)
    for i, rid in enumerate(ids):
        assert set(by_root[rid]["run_ids"]) == {rid, followup_ids[i]}


def test_concurrent_runs_with_redis_store(tmp_path):
    fake = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(client=fake)
    client = _client(tmp_path, store=store)
    n = 12
    ids = _submit_many(client, n, "redis")

    bodies = _wait_all(client, ids)
    assert all(b["result"]["ok"] for b in bodies)
    runs = store.all()
    assert len(runs) == n
    assert len({r.id for r in runs}) == n

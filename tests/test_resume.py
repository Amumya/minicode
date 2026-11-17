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
    # 检查点停在"最后一步真正执行完工具"的位置（不是被跳过的 max_steps 那一步）
    assert checkpoint["step"] == 1
    assert len(checkpoint["messages"]) >= 3  # user + assistant + tool_result
    # 关键：不能把"[skipped] 步数已用尽"写进检查点，否则续跑时模型会直接总结
    joined = repr(checkpoint["messages"])
    assert "[skipped]" not in joined
    assert checkpoint["messages"][-1]["role"] == "user"
    assert checkpoint["messages"][-1]["content"][0]["type"] == "tool_result"


def test_resume_finishes_the_run(tmp_path):
    fake = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(client=fake)
    provider = SharedProvider([
        _tool_call(1), _tool_call(2),          # 第一次跑：第 1 步真执行，第 2 步被预算截断
        _tool_call(3),                          # 续跑：第 2 步继续干
        ModelReply(text="补完了", stop_reason="end_turn"),  # 第 3 步收尾
    ])
    client = _make_app(tmp_path, store, provider)

    run_id = client.post("/runs", json={"task": "干一半"}).json()["id"]
    _wait(client, run_id)

    # 模拟进程重启：同一个 store，新的 app 实例
    client2 = _make_app(tmp_path, store, provider)
    resp = client2.post(f"/runs/{run_id}/resume", json={"max_steps": 6})
    assert resp.status_code == 202
    # 检查点停在第 1 步；续跑从第 2 步开始
    assert resp.json()["resumed_from_step"] == 1

    body = _wait(client2, run_id)
    assert body["status"] == "done"
    assert body["result"]["final_text"] == "补完了"
    # 续跑真的又执行了一次工具（不是直接总结），步数从 2 接着数
    assert body["result"]["steps"] == 3
    tool_calls = [e for e in store.read_events(run_id) if e["kind"] == "tool_call"]
    assert len(tool_calls) == 2  # 第 1 步的 t1 + 续跑第 2 步的 t3；t2 被截断没执行


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


def test_resume_409_when_already_done(tmp_path):
    """跑完的 run 不该能 resume —— 那样会在没有新用户输入的情况下再调一次模型。"""
    fake = fakeredis.FakeRedis(decode_responses=True)
    store = RedisStore(client=fake)
    provider = SharedProvider([ModelReply(text="一次就好了", stop_reason="end_turn")])
    client = _make_app(tmp_path, store, provider)

    run_id = client.post("/runs", json={"task": "简单"}).json()["id"]
    _wait(client, run_id)

    resp = client.post(f"/runs/{run_id}/resume", json={})
    assert resp.status_code == 409
    assert "followup" in resp.json()["detail"]


def test_resume_404_for_unknown_run(tmp_path):
    fake = fakeredis.FakeRedis(decode_responses=True)
    client = _make_app(tmp_path, RedisStore(client=fake), SharedProvider([]))
    assert client.post("/runs/nope/resume", json={}).status_code == 404


def test_healthz_reports_store_type(tmp_path):
    fake = fakeredis.FakeRedis(decode_responses=True)
    client = _make_app(tmp_path, RedisStore(client=fake), SharedProvider([]))
    assert client.get("/healthz").json()["store"] == "RedisStore"

def test_resume_drops_legacy_skipped_from_old_checkpoint(tmp_path):
    """兼容旧检查点：assistant(tool_use) + user([skipped]) 要在续跑前丢掉。"""
    from minicode.events import Recorder
    from minicode.hooks import HookBus
    from minicode.loop import run as run_loop
    from minicode.tools import build_registry

    seen = []

    class RecordingProvider:
        def __init__(self):
            self._round = 0

        def complete(self, system, messages, tools):
            seen.append([dict(m) for m in messages])
            self._round += 1
            return ModelReply(text="继续完成", stop_reason="end_turn")

    old_messages = [
        {"role": "user", "content": "把活干完"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t9", "name": "bash", "input": {"command": "echo hi"}},
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t9", "is_error": True,
             "content": "[skipped] 步数已用尽，这条工具调用没有执行"},
        ]},
    ]
    result = run_loop(
        "把活干完",
        provider=RecordingProvider(),
        tools=build_registry(),
        settings=Settings(api_key="fake", max_steps=2),
        workspace=tmp_path,
        hooks=HookBus(),
        recorder=Recorder(),
        resume={"messages": old_messages, "step": 2, "usage": {}},
    )
    assert result.ok
    assert len(seen) == 1
    # 续跑第一次请求里，旧的 assistant tool_use / skipped 结果已经被丢掉
    joined = repr(seen[0])
    assert "[skipped]" not in joined
    assert "t9" not in joined
    assert len(seen[0]) == 1
    assert seen[0][0]["role"] == "user"

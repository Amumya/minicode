"""RedisStore 的测试。用 fakeredis，不需要真的起一个 Redis。

这一组的重点不是"能不能读能写"，而是**换一个进程还能读到**：
同一个 fakeredis 实例喂给两个不同的 app，模拟服务重启。
"""

import fakeredis
import pytest

from minicode.service.store import RedisStore, RunState, new_id


@pytest.fixture()
def fake_client():
    return fakeredis.FakeRedis(decode_responses=True)


@pytest.fixture()
def store(fake_client):
    return RedisStore(client=fake_client, ttl_seconds=3600)


def _run(**kw) -> RunState:
    return RunState(id=new_id(), task=kw.pop("task", "做点事"),
                    model="m", workspace=".", **kw)


def test_create_and_get(store):
    run = _run()
    store.create(run)
    got = store.get(run.id)
    assert got is not None
    assert got.task == "做点事"
    assert got.status == "queued"


def test_update_status_and_result(store):
    run = _run()
    store.create(run)
    run.status = "done"
    run.result = {"ok": True, "final_text": "完事"}
    store.update(run)

    got = store.get(run.id)
    assert got.status == "done"
    assert got.result["final_text"] == "完事"


def test_events_are_ordered_and_sliceable(store):
    run = _run()
    store.create(run)
    for i in range(5):
        store.append_event(run.id, {"kind": f"e{i}", "data": {}})

    all_events = store.read_events(run.id)
    assert [e["kind"] for e in all_events] == [f"e{i}" for i in range(5)]

    tail = store.read_events(run.id, 3)
    assert [e["kind"] for e in tail] == ["e3", "e4"]

    assert store.get(run.id).event_count == 5


def test_checkpoint_roundtrip(store):
    run = _run()
    store.create(run)
    checkpoint = {"step": 3, "messages": [{"role": "user", "content": "hi"}],
                  "usage": {"input_tokens": 10, "output_tokens": 5}}
    store.save_checkpoint(run.id, checkpoint)

    got = store.get_checkpoint(run.id)
    assert got["step"] == 3
    assert got["messages"][0]["content"] == "hi"


def test_all_returns_newest_first(store):
    ids = []
    for _ in range(3):
        run = _run()
        store.create(run)
        ids.append(run.id)
    listed = [r.id for r in store.all()]
    assert listed[0] == ids[-1]


def test_data_survives_new_store_instance(fake_client):
    """换一个 RedisStore 实例（相当于服务重启）还能读到 —— 这才是"持久化"的意义。"""
    a = RedisStore(client=fake_client)
    run = _run()
    a.create(run)
    a.append_event(run.id, {"kind": "start", "data": {}})
    a.save_checkpoint(run.id, {"step": 1, "messages": [], "usage": {}})

    b = RedisStore(client=fake_client)
    assert b.get(run.id) is not None
    assert len(b.read_events(run.id)) == 1
    assert b.get_checkpoint(run.id)["step"] == 1


def test_missing_run_returns_none(store):
    assert store.get("nope") is None
    assert store.read_events("nope") == []
    assert store.get_checkpoint("nope") is None

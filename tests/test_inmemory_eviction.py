"""InMemoryStore 容量裁剪不淘汰正在执行的 run。"""

from pathlib import Path

from minicode.service.store import InMemoryStore, RunState


def _run(rid: str, status: str, tmp_path: Path) -> RunState:
    return RunState(id=rid, task="t", model="m", workspace=str(tmp_path), status=status)


def test_active_run_never_evicted(tmp_path):
    store = InMemoryStore(limit=2)
    store.create(_run("running", "running", tmp_path))
    store.create(_run("done1", "done", tmp_path))
    store.create(_run("done2", "done", tmp_path))
    assert store.get("running") is not None
    kept = [r.id for r in store.all()]
    assert "running" in kept
    assert len(kept) == 2

"""任务图（tasks）的测试。

要验的是那四条"严谨性"：
    1. 环检测：加依赖时就把环拦下，而不是让任务永远开不了工
    2. 保守依赖：前置文件读不到也算没完成
    3. 认领带 owner：别人认领的活你完不成
    4. 完成报告解锁：完成一步会告诉你因此能做什么
"""

import json
from pathlib import Path

import pytest

from minicode.tasks import STATUSES, Task, TaskStore
from minicode.tools.tasks import (
    ClaimTaskTool,
    CompleteTaskTool,
    CreateTaskTool,
    GetTaskTool,
    ListTasksTool,
    UpdateTaskTool,
    task_tools,
)


@pytest.fixture()
def store(tmp_path):
    return TaskStore(tmp_path / ".minicode" / "tasks")


# ------------------------------------------------------------------ 基本读写


def test_create_writes_file(store):
    task = store.create("读代码", "先看 src")
    assert task.id.startswith("task_")
    files = list(store.dir.glob("task_*.json"))
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["subject"] == "读代码"
    assert data["status"] == "pending"


def test_empty_subject_rejected(store):
    with pytest.raises(ValueError):
        store.create("   ")


def test_survives_new_store_instance(store, tmp_path):
    task = store.create("持久化")
    again = TaskStore(tmp_path / ".minicode" / "tasks")
    assert again.load(task.id).subject == "持久化"


def test_list_sorted_by_created(store):
    a = store.create("A")
    b = store.create("B")
    assert [t.id for t in store.list()] == [a.id, b.id]


def test_bad_id_rejected(store):
    with pytest.raises(ValueError):
        store.load("../../etc/passwd")
    with pytest.raises(ValueError):
        store.load("not_a_task")


# ------------------------------------------------------------------ 依赖与环


def test_dependency_blocks_start(store):
    a = store.create("A")
    b = store.create("B", blocked_by=[a.id])
    assert store.incomplete_dependencies(b) == [a.id]
    assert not store.can_start(b)
    with pytest.raises(ValueError):
        store.claim(b.id)


def test_missing_dependency_counts_as_blocked(store, tmp_path):
    """前置文件被删掉时按"没完成"处理 —— 宁可不开工，也不要在状态不明时放行。"""
    a = store.create("A")
    b = store.create("B", blocked_by=[a.id])
    (store.dir / f"{a.id}.json").unlink()
    assert store.incomplete_dependencies(b) == [a.id]
    assert not store.can_start(b)


def test_cycle_rejected_on_update(store):
    a = store.create("A")
    b = store.create("B", blocked_by=[a.id])
    with pytest.raises(ValueError) as exc:
        store.update_dependencies(a.id, [b.id])
    assert "成环" in str(exc.value)


def test_self_dependency_rejected(store):
    a = store.create("A")
    with pytest.raises(ValueError):
        store.update_dependencies(a.id, [a.id])


def test_missing_dependency_on_update(store):
    a = store.create("A")
    with pytest.raises(ValueError):
        store.update_dependencies(a.id, ["task_deadbeef"])


# ------------------------------------------------------------------ 认领与完成


def test_claim_then_complete_unlocks(store):
    a = store.create("A")
    b = store.create("B", blocked_by=[a.id])

    claimed = store.claim(a.id, "alice")
    assert claimed.status == "in_progress"
    assert claimed.owner == "alice"

    done, unlocked = store.complete(a.id, "alice")
    assert done.status == "completed"
    assert unlocked == [b.id]
    assert store.can_start(b)


def test_complete_by_wrong_owner_rejected(store):
    a = store.create("A")
    store.claim(a.id, "alice")
    with pytest.raises(ValueError) as exc:
        store.complete(a.id, "bob")
    assert "alice" in str(exc.value)


def test_claim_twice_rejected(store):
    a = store.create("A")
    store.claim(a.id)
    with pytest.raises(ValueError):
        store.claim(a.id)


def test_complete_pending_rejected(store):
    a = store.create("A")
    with pytest.raises(ValueError):
        store.complete(a.id)


def test_claim_unknown_task(store):
    with pytest.raises(FileNotFoundError):
        store.claim("task_deadbeef")


# ------------------------------------------------------------------ 渲染


def test_render_list_empty(store):
    assert "还没有任务" in store.render_list()


def test_render_list_marks_and_waiting(store):
    a = store.create("A")
    b = store.create("B", blocked_by=[a.id])
    store.claim(a.id, "alice")
    text = store.render_list()
    assert "[~] " + a.id in text
    assert "alice" in text
    assert "← 等" in text
    assert b.id in text


# ------------------------------------------------------------------ 工具的包装


def test_tools_are_six(store):
    assert len(task_tools(store)) == 6
    names = {t.name for t in task_tools(store)}
    assert names == {"create_task", "update_task", "list_tasks",
                     "get_task", "claim_task", "complete_task"}


def test_tool_create_and_flow(tmp_path, store):
    created = CreateTaskTool(store).run({"subject": "写代码"}, tmp_path)
    assert created.exit_code == 0
    task_id = created.meta["id"]

    assert "写代码" in ListTasksTool(store).run({}, tmp_path).content
    detail = GetTaskTool(store).run({"taskId": task_id}, tmp_path)
    assert "unfinishedDependencies" in detail.content

    assert ClaimTaskTool(store).run({"taskId": task_id}, tmp_path).exit_code == 0
    done = CompleteTaskTool(store).run({"taskId": task_id}, tmp_path)
    assert done.exit_code == 0


def test_tool_create_rejects_missing_dependency(tmp_path, store):
    r = CreateTaskTool(store).run(
        {"subject": "B", "blockedBy": ["task_deadbeef"]}, tmp_path)
    assert r.exit_code == 1
    assert "不存在" in r.content


def test_tool_update_cycle(tmp_path, store):
    a = store.create("A")
    b = store.create("B", blocked_by=[a.id])
    r = UpdateTaskTool(store).run(
        {"taskId": a.id, "addBlockedBy": [b.id]}, tmp_path)
    assert r.exit_code == 1
    assert "成环" in r.content


def test_tool_complete_reports_unlock(tmp_path, store):
    a = store.create("A")
    b = store.create("B", blocked_by=[a.id])
    ClaimTaskTool(store).run({"taskId": a.id}, tmp_path)
    r = CompleteTaskTool(store).run({"taskId": a.id}, tmp_path)
    assert "现在可以做" in r.content
    assert b.id in r.content


def test_tool_errors_are_readable(tmp_path, store):
    r = ClaimTaskTool(store).run({"taskId": "task_deadbeef"}, tmp_path)
    assert r.exit_code == 1
    assert "没有这个任务" in r.content


def test_public_dict_shape(store):
    t = store.create("A", "说明")
    pub = t.to_public()
    assert set(pub) == {"id", "subject", "description", "status", "owner", "blockedBy"}
    assert set(STATUSES) == {"pending", "in_progress", "completed"}

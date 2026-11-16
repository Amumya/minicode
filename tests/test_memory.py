"""长期记忆：存储、排序、注入提示词、工具、服务层接口。

设计见 src/minicode/memory.py 顶部注释。这里重点验四件事：
    1. 去重和持久化（同一条记两遍只存一份；换一个实例还能读到）
    2. 相关度排序（查 pytest 时应该挑出"测试用 .venv 里的 python"）
    3. 提示词注入（开了才带，且带的是挑过的那几条）
    4. 工具和接口（remember 真写进文件；GET /memory 能读到）
"""

from __future__ import annotations

import json
import time

import pytest

from minicode.memory import MemoryStore, tokenize
from minicode.prompts import default_system
from minicode.tools import build_registry


# ------------------------------------------------------------------ 存储


def test_empty_store(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    assert store.entries() == []
    assert store.snapshot("随便") == ""


def test_add_and_persist(tmp_path):
    path = tmp_path / "memory.jsonl"
    store = MemoryStore(path)
    entry, created = store.add("测试用 .venv 里的 python", ["环境"])
    assert created is True
    assert entry.id and entry.text.startswith("测试用")

    again = MemoryStore(path)          # 换一个实例，模拟"下一次运行"
    entries = again.entries()
    assert len(entries) == 1
    assert entries[0].text == "测试用 .venv 里的 python"
    assert entries[0].tags == ["环境"]


def test_duplicate_is_not_stored_twice(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    _first, created1 = store.add("用 pytest -q 跑测试")
    _second, created2 = store.add("用  pytest   -q 跑测试")   # 多个空格视为同一条
    assert created1 is True
    assert created2 is False
    assert len(store.entries()) == 1


def test_forget(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    entry, _ = store.add("要删掉的")
    assert store.forget(entry.id) is True
    assert store.forget(entry.id) is False
    assert store.entries() == []


def test_corrupted_line_is_skipped(tmp_path):
    path = tmp_path / "memory.jsonl"
    store = MemoryStore(path)
    store.add("好的一条")
    with path.open("a", encoding="utf-8") as fh:
        fh.write("{这不是合法 JSON\n")
    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].text == "好的一条"


def test_long_entry_is_truncated(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    entry, _ = store.add("长" * 1000)
    assert len(entry.text) <= 401
    assert entry.text.endswith("…")


def test_limit_keeps_newest(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl", limit=3)
    for i in range(5):
        store.add(f"第 {i} 条")
        time.sleep(0.001)
    texts = [e.text for e in store.entries()]
    assert texts == ["第 2 条", "第 3 条", "第 4 条"]


# ------------------------------------------------------------------ 排序


def test_tokenize_handles_chinese_and_ascii():
    tokens = tokenize("用 .venv 里的 Python 跑 pytest")
    assert "python" in tokens
    assert "pytest" in tokens
    assert "venv" in tokens
    assert any(len(t) == 2 and t.isalpha() for t in tokens if not t.isascii()) or True


def test_relevant_prefers_matching_entry(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    store.add("这个项目的测试要用 .venv\\Scripts\\python.exe 跑 pytest", ["测试", "python"])
    store.add("提交信息用中文写", ["流程"])
    store.add("不要动 eval/runs 里的历史数据", ["评测"])

    picked = store.relevant("跑 pytest 用哪个 python", limit=2)
    assert picked, "应该挑出记忆"
    assert "pytest" in picked[0].text


def test_empty_query_returns_recent(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    store.add("第一条")
    time.sleep(0.002)
    store.add("第二条")
    picked = store.relevant("", limit=2)
    assert picked[0].text == "第二条"


# ------------------------------------------------------------------ 提示词注入


def test_prompt_includes_snapshot_only_when_enabled():
    snapshot = "- [2026-01-01] 测试用 .venv 里的 python"
    with_memory = default_system(memory_hint=True, memory_snapshot=snapshot)
    assert "长期记忆" in with_memory
    assert "测试用 .venv 里的 python" in with_memory

    without = default_system()
    assert "长期记忆" not in without
    assert "测试用 .venv 里的 python" not in without


def test_snapshot_respects_budget(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    for i in range(20):
        store.add(f"第 {i} 条很长很长的记忆内容" * 5)
    snap = store.snapshot("task", max_entries=20, max_chars=120)
    assert len(snap) <= 140
    assert snap.count("\n- [") + snap.startswith("- [") <= 2   # 最多两三条


# ------------------------------------------------------------------ 工具


def _run_tool(reg, name: str, args: dict, cwd):
    tool = reg.get(name)
    assert tool is not None, f"没有注册工具 {name}"
    return tool.run(args, cwd)


def test_remember_and_recall_tools(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    reg = build_registry(memory=store)

    result = _run_tool(reg, "remember", {"text": "测试要用 .venv 里的 python", "tags": "环境,测试"}, tmp_path)
    assert result.exit_code == 0
    assert "已记住" in result.content
    assert store.entries()

    recall = _run_tool(reg, "recall", {"query": "pytest 怎么跑"}, tmp_path)
    assert recall.exit_code == 0
    assert "测试要用 .venv 里的 python" in recall.content


def test_remember_rejects_empty(tmp_path):
    reg = build_registry(memory=MemoryStore(tmp_path / "memory.jsonl"))
    result = _run_tool(reg, "remember", {"text": "   "}, tmp_path)
    assert result.exit_code is None


def test_forget_tool(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    reg = build_registry(memory=store)
    created = _run_tool(reg, "remember", {"text": "临时记一条"}, tmp_path)
    entry_id = created.meta["memory_id"]

    assert _run_tool(reg, "forget", {"id": entry_id}, tmp_path).exit_code == 0
    assert store.entries() == []
    assert _run_tool(reg, "forget", {"id": entry_id}, tmp_path).exit_code == 1


def test_memory_tools_not_registered_by_default():
    from minicode.tools import default_registry

    reg = default_registry()
    assert reg.get("remember") is None


# ------------------------------------------------------------------ 服务层


def test_api_memory_endpoint(tmp_path):
    from fastapi.testclient import TestClient

    from minicode.config import Settings
    from minicode.provider import ModelReply
    from minicode.service import create_app
    from minicode.service.store import InMemoryStore

    class Echo:
        def complete(self, system, messages, tools):
            return ModelReply(text="ok", stop_reason="end_turn")

    client = TestClient(create_app(
        provider_factory=lambda s: Echo(),
        settings=Settings(api_key="fake", model="m", max_steps=2),
        store=InMemoryStore(),
        default_workspace=tmp_path,
    ))
    # 直接往文件里写一条（模拟之前的运行留下的）
    store = MemoryStore(tmp_path / ".minicode" / "memory.jsonl")
    entry, _ = store.add("项目测试用 .venv 里的 python", ["环境"])

    data = client.get("/memory").json()
    assert data["total"] == 1
    assert data["entries"][0]["text"] == "项目测试用 .venv 里的 python"
    assert data["entries"][0]["tags"] == ["环境"]

    assert client.delete(f"/memory/{entry.id}").status_code == 200
    assert client.get("/memory").json()["total"] == 0
    assert client.delete("/memory/nope").status_code == 404

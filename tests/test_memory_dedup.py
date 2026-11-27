"""长期记忆三层判重：严格指纹 → Jaccard → 模型裁决/合并。"""

from __future__ import annotations

from minicode.memory import (
    LLMMemoryJudge,
    MemoryStore,
    jaccard,
)


class FakeJudge:
    def __init__(self, decision: str = "different", merged: str = "") -> None:
        self.decision = decision
        self.merged = merged
        self.decide_calls: list[tuple[str, str]] = []
        self.merge_calls: list[tuple[str, str]] = []

    def decide(self, new_text: str, existing_text: str) -> str:
        self.decide_calls.append((new_text, existing_text))
        return self.decision

    def merge(self, new_text: str, existing_text: str) -> str:
        self.merge_calls.append((new_text, existing_text))
        return self.merged


def test_exact_duplicate_merges_tags(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    first, created = store.add("用 pytest -q 跑测试", ["old"])
    again, created_again = store.add("用  pytest   -q 跑测试", ["new"])

    assert created is True
    assert created_again is False
    assert again.id == first.id
    assert len(store.entries()) == 1
    assert set(store.entries()[0].tags) == {"old", "new"}


def test_jaccard_at_least_09_is_duplicate(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    a = "用 pytest -q 跑测试，不要用 unittest"
    b = "用 pytest -q 跑测试，不要用 unittest。"
    assert jaccard(a, b) >= 0.9

    first, _ = store.add(a)
    again, created = store.add(b)

    assert created is False
    assert again.id == first.id
    assert len(store.entries()) == 1


def test_jaccard_between_thresholds_calls_judge_same(tmp_path):
    judge = FakeJudge(decision="same")
    store = MemoryStore(tmp_path / "memory.jsonl", judge=judge)
    first, _ = store.add("项目测试用 .venv 里的 python")
    again, created = store.add("项目测试要用 .venv 里的 python")

    assert 0.7 <= jaccard("项目测试用 .venv 里的 python", "项目测试要用 .venv 里的 python") < 0.9
    assert judge.decide_calls
    assert created is False
    assert again.id == first.id


def test_judge_merge_updates_existing_entry(tmp_path):
    judge = FakeJudge(
        decision="merge",
        merged="项目测试统一用 .venv 里的 python 跑 pytest",
    )
    store = MemoryStore(tmp_path / "memory.jsonl", judge=judge)
    first, _ = store.add("默认使用 PowerShell 执行命令")
    again, created = store.add("默认用 PowerShell 执行命令")

    assert judge.merge_calls
    assert created is False
    assert again.id == first.id
    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].text == "项目测试统一用 .venv 里的 python 跑 pytest"


def test_judge_different_adds_new_entry(tmp_path):
    judge = FakeJudge(decision="different")
    store = MemoryStore(tmp_path / "memory.jsonl", judge=judge)
    store.add("项目测试用 .venv 里的 python")
    _entry, created = store.add("项目测试要用 .venv 里的 python")

    assert created is True
    assert len(store.entries()) == 2


def test_no_judge_falls_back_to_add(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    store.add("项目测试用 .venv 里的 python")
    _entry, created = store.add("项目测试要用 .venv 里的 python")

    # 没有模型时第三层不能擅自判“同一条”，否则会丢新事实
    assert created is True
    assert len(store.entries()) == 2


def test_low_jaccard_adds_directly(tmp_path):
    judge = FakeJudge(decision="same")
    store = MemoryStore(tmp_path / "memory.jsonl", judge=judge)
    store.add("发布前需要跑完整测试")
    _entry, created = store.add("提交信息用中文写")

    assert created is True
    assert not judge.decide_calls
    assert len(store.entries()) == 2


def test_llm_judge_only_same_different_merge():
    class Reply:
        def __init__(self, text: str) -> None:
            self.text = text

    class Provider:
        def __init__(self, answer: str) -> None:
            self.answer = answer
            self.calls: list[tuple[str, list[dict], list[dict]]] = []

        def complete(self, system, messages, tools):
            self.calls.append((system, messages, tools))
            return Reply(self.answer)

    for answer, expected in (("same", "same"), ("different", "different"),
                             ("merge", "merge"), ("我觉得一样", "different")):
        provider = Provider(answer)
        judge = LLMMemoryJudge(provider)
        assert judge.decide("新", "旧") == expected
        assert provider.calls

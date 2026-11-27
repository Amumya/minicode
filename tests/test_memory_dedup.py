"""长期记忆三层判重：严格指纹 → top-k 召回 → 模型裁决。"""

from __future__ import annotations

from minicode.memory import (
    LLMMemoryJudge,
    MemoryStore,
    jaccard,
)

# 真实回归对：0.1556，旧版因为这个分数低于 0.7 而漏判
OLD_IDENTITY = (
    '用户是本助手（助手项目）的作者/开发者本人，即"我的作者"，'
    "回答时可以按这个关系称呼和展开。"
)
NEW_IDENTITY = (
    '用户是本助手项目的开发者本人（作者），2026-11-24 再次明确要求记住这一点；'
    '回答可直接以"你的项目"视角展开，不用客套。'
)
PAIR_040 = (
    "40-step 顺序执行探针放在 .minicode/step40/step_runner.py（python step_runner.py N，N=1..40）："
    "每步读上一步日志算 sha256 做哈希链，写 steps/step_NN.log，缺步或重复跑会 exit 1；"
    "2026-11-18 跑满 40 步（alpha..opal），40 个文件共 11404 字节，跨约 20 秒，链校验 0 错误。"
)
PAIR_COUNT40 = (
    ".minicode/count40/count_step.py 是 40 步纯计数探针（python count_step.py N，N=1..40）："
    "只做计数 +1、写 counter.txt 和 steps/step_NN.log，无哈希链，步序不对 exit 1；"
    "2026-11-18 跑满 40 步，计数 1..40 连续、40 个文件。"
)
PAIR_400 = (
    'minicode 会话报 400 "The content[].thinking in the thinking mode must be passed back to the API"：'
    'deepseek-flash 走 /anthropic 兼容端点时是 thinking 模式，assistant 历史必须原样回传 '
    'thinking/redacted_thinking 块；目前 provider.py 的 ModelReply 只有 text/tool_calls，'
    '_to_reply 和 loop.py::_assistant_message 把 thinking 丢了。'
)
PAIR_400_FIXED = (
    'minicode 的 thinking 回传 bug 已修：provider.py 的 ModelReply 现在有 thinking 字段、'
    '_to_reply 会收集 thinking/redacted_thinking 块，loop.py::_assistant_message 用 '
    'list(reply.thinking) 原样放在 assistant content 最前面——之前那条"thinking 被丢掉导致 400"的记忆已过期。'
)


class FakeJudge:
    def __init__(self, decision: str = "different") -> None:
        self.decision = decision
        self.calls: list[tuple[str, list[str]]] = []

    def decide(self, new_text: str, candidates: list[str]) -> str:
        self.calls.append((new_text, candidates))
        return self.decision


def test_exact_duplicate_merges_tags(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    first, created = store.add("用 pytest -q 跑测试", ["old"])
    again, created_again = store.add("用  pytest   -q 跑测试", ["new"])

    assert created is True
    assert created_again is False
    assert again.id == first.id
    assert len(store.entries()) == 1
    assert set(store.entries()[0].tags) == {"old", "new"}


def test_jaccard_at_least_09_is_duplicate_without_judge(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    a = "用 pytest -q 跑测试，不要用 unittest"
    b = "用 pytest -q 跑测试，不要用 unittest。"
    assert jaccard(a, b) >= 0.9

    first, _ = store.add(a)
    again, created = store.add(b)

    assert created is False
    assert again.id == first.id
    assert len(store.entries()) == 1


def test_low_jaccard_semantic_duplicate_reaches_judge(tmp_path):
    """0.1556 的真实同义改写必须进入 judge，而不是被 0.7 拦住。"""
    judge = FakeJudge("same")
    store = MemoryStore(tmp_path / "memory.jsonl", judge=judge)
    first, _ = store.add(OLD_IDENTITY)
    again, created = store.add(NEW_IDENTITY)

    assert 0.05 <= jaccard(NEW_IDENTITY, OLD_IDENTITY) < 0.7
    assert judge.calls, "低分候选也必须交给 judge"
    assert len(judge.calls[0][1]) >= 1
    assert OLD_IDENTITY in judge.calls[0][1]
    assert created is False
    assert again.id == first.id


def test_judge_replace_overwrites_existing(tmp_path):
    judge = FakeJudge("replace")
    store = MemoryStore(tmp_path / "memory.jsonl", judge=judge)
    first, _ = store.add(OLD_IDENTITY, ["身份"])
    again, created = store.add(NEW_IDENTITY, ["偏好"])

    assert created is False
    assert again.id == first.id
    entries = store.entries()
    assert len(entries) == 1
    assert entries[0].text == NEW_IDENTITY
    assert set(entries[0].tags) == {"身份", "偏好"}


def test_judge_same_keeps_old_text(tmp_path):
    judge = FakeJudge("same")
    store = MemoryStore(tmp_path / "memory.jsonl", judge=judge)
    first, _ = store.add(OLD_IDENTITY)
    again, created = store.add(NEW_IDENTITY, ["新标签"])

    assert created is False
    assert again.id == first.id
    assert store.entries()[0].text == OLD_IDENTITY
    assert "新标签" in store.entries()[0].tags


def test_judge_skip_does_not_write(tmp_path):
    judge = FakeJudge("skip")
    store = MemoryStore(tmp_path / "memory.jsonl", judge=judge)
    first, _ = store.add(OLD_IDENTITY)
    again, created = store.add(NEW_IDENTITY, ["新标签"])

    assert created is False
    assert again.id == first.id
    assert store.entries()[0].text == OLD_IDENTITY
    assert "新标签" not in store.entries()[0].tags


def test_judge_different_adds_new_entry(tmp_path):
    judge = FakeJudge("different")
    store = MemoryStore(tmp_path / "memory.jsonl", judge=judge)
    store.add("项目测试用 .venv 里的 python")
    entry, created = store.add("项目测试要用 .venv 里的 python")

    assert created is True
    assert entry.id not in {e.id for e in store.entries() if e.text != entry.text}
    assert len(store.entries()) == 2


def test_negative_high_scoring_pairs_are_not_merged(tmp_path):
    """0.397/0.386 的语料对是真不同；放宽召回后 judge 必须能保住它们。"""
    for left, right in ((PAIR_040, PAIR_COUNT40), (PAIR_400, PAIR_400_FIXED)):
        judge = FakeJudge("different")
        store = MemoryStore(tmp_path / "memory.jsonl", judge=judge)
        store.add(left)
        _entry, created = store.add(right)

        assert jaccard(left, right) >= 0.05
        assert judge.calls
        assert created is True
        assert len(store.entries()) == 2
        (tmp_path / "memory.jsonl").unlink()


def test_low_score_below_recall_floor_skips_judge(tmp_path):
    judge = FakeJudge("same")
    store = MemoryStore(tmp_path / "memory.jsonl", judge=judge)
    store.add("alpha beta")
    _entry, created = store.add("一丁二丙")

    assert jaccard("alpha beta", "一丁二丙") < 0.05
    assert not judge.calls
    assert created is True
    assert len(store.entries()) == 2


def test_no_judge_falls_back_to_add(tmp_path):
    store = MemoryStore(tmp_path / "memory.jsonl")
    store.add(OLD_IDENTITY)
    _entry, created = store.add(NEW_IDENTITY)

    # 没有模型时，低分候选不能擅自判“同一条”，否则会丢新事实
    assert created is True
    assert len(store.entries()) == 2


def test_top_k_candidates_are_passed_to_judge(tmp_path):
    judge = FakeJudge("different")
    store = MemoryStore(tmp_path / "memory.jsonl", judge=judge)
    for text in ("alpha one", "alpha two", "alpha three", "alpha four"):
        store.add(text)
    store.add("alpha one updated")

    assert judge.calls
    candidates = judge.calls[0][1]
    assert 1 <= len(candidates) <= 3


def test_llm_judge_only_four_answers():
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

    for answer, expected in (
        ("same", "same"),
        ("replace", "replace"),
        ("different", "different"),
        ("skip", "skip"),
        ("我觉得一样", "different"),
    ):
        provider = Provider(answer)
        judge = LLMMemoryJudge(provider)
        assert judge.decide("新", ["旧一", "旧二"]) == expected
        assert provider.calls


def test_llm_judge_uses_stream_when_provider_has_no_complete():
    class Reply:
        def __init__(self, text: str) -> None:
            self.text = text

    class StreamProvider:
        def __init__(self) -> None:
            self.calls: list[tuple[str, list[dict], list[dict]]] = []

        def stream(self, system, messages, tools, on_text):
            self.calls.append((system, messages, tools))
            on_text("replace")
            return Reply("replace")

    provider = StreamProvider()
    judge = LLMMemoryJudge(provider)
    assert judge.decide("新", ["旧"]) == "replace"
    assert provider.calls

"""上下文压缩的测试（五级阶梯版）。

核心不变式不变：
    1. 压缩后**不能出现孤立的 tool_use**（assistant 说调工具，但没有对应的 tool_result）
    2. 压缩后**不能出现孤立的 tool_result**
这两条一旦破了，下一次请求就是 400，而且报错信息跟压缩毫无关系。
"""

from pathlib import Path

from minicode.compact import CompactConfig, Compactor, messages_chars


def _assistant_tool_use(tool_id: str, text: str = "") -> dict:
    return {
        "role": "assistant",
        "content": [{"type": "tool_use", "id": tool_id, "name": "bash", "input": {}}],
    }


def _user_tool_result(tool_id: str, text: str) -> dict:
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": text}],
    }


def _conversation(steps: int, size: int = 100) -> list[dict]:
    msgs = [{"role": "user", "content": "任务"}]
    for i in range(steps):
        msgs.append(_assistant_tool_use(f"t{i}"))
        msgs.append(_user_tool_result(f"t{i}", "x" * size))
    return msgs


def _pairs_ok(messages: list[dict]) -> bool:
    uses = set()
    results = set()
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        for b in content:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                uses.add(b["id"])
            elif b.get("type") == "tool_result":
                results.add(b["tool_use_id"])
    # 允许"结果在更早的已被裁掉的历史里"，但绝不能有"有请求没结果"
    return uses <= results


def _all_blocks(messages: list[dict]) -> list[dict]:
    blocks = []
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            blocks.extend(b for b in content if isinstance(b, dict))
    return blocks


# ---------------------------------------------------------------- L0 无操作

def test_small_input_untouched(tmp_path):
    msgs = _conversation(1, size=10)
    out, stats = Compactor(tmp_path).prepare(msgs, "任务")
    assert out == msgs
    assert not stats.changed
    assert stats.level == "none"


def test_disabled_config_is_noop(tmp_path):
    msgs = _conversation(30, size=50_000)
    out, stats = Compactor(tmp_path, CompactConfig(enabled=False)).prepare(msgs, "任务")
    assert out == msgs
    assert not stats.changed


def test_empty_messages(tmp_path):
    out, stats = Compactor(tmp_path).prepare([], "任务")
    assert out == []
    assert not stats.changed


def test_string_content_messages_untouched(tmp_path):
    msgs = [{"role": "user", "content": "任务"}, {"role": "assistant", "content": "回答"}]
    out, _ = Compactor(tmp_path).prepare(msgs, "任务")
    assert out == msgs


# ---------------------------------------------------------------- L1 落盘

def test_large_output_is_persisted(tmp_path):
    msgs = _conversation(1, size=50_000)
    comp = Compactor(tmp_path, CompactConfig(max_tool_result_chars=1000))
    out, stats = comp.prepare(msgs, "任务")
    assert stats.persisted == 1
    assert stats.level == "persist"
    text = out[2]["content"][0]["content"]
    assert "<persisted-output>" in text
    assert "Full output:" in text
    assert len(text) < 3000
    # 落盘文件实际存在，且内容完整。
    files = list((tmp_path / ".minicode" / "tool_results").glob("*.txt"))
    assert len(files) == 1
    assert len(files[0].read_text(encoding="utf-8")) == 50_000


# ---------------------------------------------------------------- L2 归档

def test_snip_archives_middle(tmp_path):
    msgs = _conversation(20, size=10)
    comp = Compactor(tmp_path, CompactConfig(max_messages=10, max_tool_result_chars=100_000))
    out, stats = comp.prepare(msgs, "任务")
    assert stats.snipped_messages > 0
    assert len(out) <= 10
    # 第 1 条必须还在（任务本身）
    assert out[0] == msgs[0]
    assert _pairs_ok(out), "压缩后出现了没有结果的 tool_use"
    assert any("条消息已归档至" in str(m.get("content")) for m in out)
    # 归档文件真实存在
    assert list((tmp_path / ".minicode" / "transcripts").glob("*.jsonl"))


def test_snip_never_leaves_orphan_results(tmp_path):
    """删的必须是整段历史，不能留下没有 tool_use 的 tool_result。"""
    msgs = _conversation(12, size=10)
    comp = Compactor(tmp_path, CompactConfig(max_messages=8, max_tool_result_chars=100_000))
    out, _ = comp.prepare(msgs, "任务")
    uses = {b["id"] for b in _all_blocks(out) if b.get("type") == "tool_use"}
    for b in _all_blocks(out):
        if b.get("type") == "tool_result":
            assert b["tool_use_id"] in uses


# ---------------------------------------------------------------- L3 微压缩

def test_batch_over_with_medium_block_is_not_counted_as_persisted(tmp_path):
    # 单条 500 字符没超 max_tool_result_chars，只触发 batch 超限：
    # persist_large_output 原样返回，不得虚报 persisted。
    msgs = _conversation(1, size=500)
    comp = Compactor(tmp_path, CompactConfig(max_tool_result_chars=1000, batch_result_chars=100))
    out, stats = comp.prepare(msgs, "任务")
    assert stats.persisted == 0
    assert not stats.changed
    assert out[2]["content"][0]["content"] == "x" * 500


def test_micro_compact_trims_old_keeps_recent(tmp_path):
    msgs = _conversation(5, size=20_000)
    comp = Compactor(
        tmp_path,
        CompactConfig(
            max_tool_result_chars=100_000,   # 关掉 L1，只测 L3
            keep_recent_results=2,
            context_char_limit=50_000,
        ),
    )
    out, stats = comp.prepare(msgs, "任务")
    assert stats.trimmed > 0
    assert stats.level == "micro"
    blocks = [m["content"][0] for m in out if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(blocks) == 5
    # 最新的结果（模型还没读过）保留全文；旧的被换成落盘指针
    assert "[Earlier tool result saved at " in blocks[0]["content"]
    assert len(blocks[-1]["content"]) == 20_000
    assert "Earlier tool result" not in blocks[-1]["content"]


# ---------------------------------------------------------------- L4 摘要

def test_summary_compacts_history(tmp_path):
    msgs = _conversation(4, size=40_000)
    comp = Compactor(
        tmp_path,
        CompactConfig(
            max_tool_result_chars=100_000,   # 关掉 L1
            context_char_limit=20_000,       # 逼到 L4
        ),
        summarize=lambda prompt: "这是压缩后的摘要",
    )
    out, stats = comp.prepare(msgs, "任务")
    assert stats.summarized
    assert stats.level == "summary"
    assert len(out) == 3
    assert out[-1]["content"][0]["type"] == "tool_result"
    assert len(out[-1]["content"][0]["content"]) == 40_000
    assert "这是压缩后的摘要" in str(out[0]["content"])


def test_reactive_compact_keeps_recent(tmp_path):
    msgs = _conversation(6, size=1_000)
    comp = Compactor(
        tmp_path,
        CompactConfig(),
        summarize=lambda prompt: "反应式摘要",
    )
    out, stats = comp.reactive_compact(msgs, "任务")
    assert stats.summarized
    assert stats.level == "reactive"
    assert "反应式摘要" in str(out[0]["content"])


# ---------------------------------------------------------------- 统计与工具函数

def test_stats_report_savings(tmp_path):
    msgs = _conversation(1, size=50_000)
    comp = Compactor(tmp_path, CompactConfig(max_tool_result_chars=1000))
    _, stats = comp.prepare(msgs, "任务")
    d = stats.as_dict()
    assert d["chars_before"] > d["chars_after"]
    assert d["saved_ratio"] > 0.5
    assert d["level"] != "none"


def test_stats_unchanged_when_nothing_to_do(tmp_path):
    msgs = _conversation(1, size=10)
    _, stats = Compactor(tmp_path).prepare(msgs, "任务")
    assert stats.as_dict()["saved_chars"] == 0
    assert "未压缩" in Compactor(tmp_path).summary_line(stats)


def test_messages_chars_counts_nested():
    """字符统计是粗估：会把 id / name 这些字段也算进去，所以只断言下界。"""
    msgs = _conversation(2, size=100)
    assert messages_chars(msgs) >= 200 + len("任务")

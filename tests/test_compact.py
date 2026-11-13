"""上下文压缩的测试。

核心是两条不变式：
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


def test_small_input_untouched(tmp_path):
    msgs = _conversation(1, size=10)
    out, stats = Compactor(tmp_path).compact(msgs)
    assert out == msgs
    assert not stats.changed


def test_large_output_is_persisted(tmp_path):
    msgs = _conversation(1, size=50_000)
    comp = Compactor(tmp_path, CompactConfig(max_tool_result_chars=1000))
    out, stats = comp.compact(msgs)
    assert stats.persisted == 1
    text = out[2]["content"][0]["content"]
    assert "完整内容在" in text
    assert len(text) < 2000
    # 落盘文件真的存在，而且内容完整
    files = list((tmp_path / ".minicode" / "tool_results").glob("*.txt"))
    assert len(files) == 1
    assert len(files[0].read_text(encoding="utf-8")) == 50_000


def test_recent_results_kept_old_trimmed(tmp_path):
    msgs = _conversation(5, size=2000)
    comp = Compactor(tmp_path, CompactConfig(
        max_tool_result_chars=100_000,   # 关掉落盘，只测截断
        keep_recent_tool_results=2,
        old_tool_result_chars=100,
    ))
    out, stats = comp.compact(msgs)
    assert stats.trimmed == 3
    blocks = [m["content"][0] for m in out if m["role"] == "user" and isinstance(m["content"], list)]
    assert len(blocks) == 5
    assert "已截断" in blocks[0]["content"]
    assert "已截断" not in blocks[-1]["content"]
    assert len(blocks[-1]["content"]) == 2000


def test_snip_drops_whole_pairs(tmp_path):
    msgs = _conversation(20, size=10)
    comp = Compactor(tmp_path, CompactConfig(max_messages=10, max_tool_result_chars=100_000))
    out, stats = comp.compact(msgs)
    assert stats.snipped_messages > 0
    assert len(out) <= 10
    # 第 1 条必须还在（任务本身）
    assert out[0] == msgs[0]
    assert _pairs_ok(out), "压缩后出现了没有结果的 tool_use"


def test_snip_never_leaves_orphan_results(tmp_path):
    """删的必须是 assistant+user 整对，不能只删 assistant。"""
    msgs = _conversation(12, size=10)
    comp = Compactor(tmp_path, CompactConfig(max_messages=8, max_tool_result_chars=100_000))
    out, _ = comp.compact(msgs)
    for m in out:
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    assert b["tool_use_id"] in {
                        bb["id"]
                        for mm in out
                        for bb in (mm.get("content") or [])
                        if isinstance(bb, dict) and bb.get("type") == "tool_use"
                    }


def test_disabled_config_is_noop(tmp_path):
    msgs = _conversation(30, size=50_000)
    out, stats = Compactor(tmp_path, CompactConfig(enabled=False)).compact(msgs)
    assert out == msgs
    assert not stats.changed


def test_stats_report_savings(tmp_path):
    msgs = _conversation(4, size=20_000)
    comp = Compactor(tmp_path, CompactConfig(max_tool_result_chars=1000))
    _, stats = comp.compact(msgs)
    d = stats.as_dict()
    assert d["chars_before"] > d["chars_after"]
    assert d["saved_ratio"] > 0.5


def test_stats_unchanged_when_nothing_to_do(tmp_path):
    msgs = _conversation(1, size=10)
    _, stats = Compactor(tmp_path).compact(msgs)
    assert stats.as_dict()["saved_chars"] == 0
    assert "未压缩" in Compactor(tmp_path).summary_line(stats)


def test_messages_chars_counts_nested():
    """字符统计是粗估：会把 id / name 这些字段也算进去，所以只断言下界。"""
    msgs = _conversation(2, size=100)
    assert messages_chars(msgs) >= 200 + len("任务")


def test_empty_messages(tmp_path):
    out, stats = Compactor(tmp_path).compact([])
    assert out == []
    assert not stats.changed


def test_string_content_messages_untouched(tmp_path):
    msgs = [{"role": "user", "content": "任务"}, {"role": "assistant", "content": "回答"}]
    out, _ = Compactor(tmp_path).compact(msgs)
    assert out == msgs

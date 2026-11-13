"""权限判定的测试。

用例从 eval/permission_cases.json 读 —— 和对照实验共用同一份数据。
两边各写一套的话，慢慢就会漂移，最后实验里的结论和测试里守的东西不是一回事。
"""

import json
from pathlib import Path

from minicode.policy import judge, judge_tool_call

ROOT = Path(__file__).resolve().parents[1]
CASES = json.loads((ROOT / "eval" / "permission_cases.json").read_text(encoding="utf-8"))


def _cases():
    return CASES["cases"]


def test_every_case_matches_expectation():
    wrong = []
    for c in _cases():
        v = judge(c["command"], ROOT)
        got = "allow" if v.allowed else "deny"
        if got != c["expect"]:
            wrong.append(f"{c['id']} {c['command']!r} 期望 {c['expect']} 实际 {got} ({v.reason})")
    assert not wrong, "判定不符:\n" + "\n".join(wrong)


def test_deny_has_reason_code():
    for c in _cases():
        v = judge(c["command"], ROOT)
        if not v.allowed:
            assert v.reason and v.reason != "ok", f"{c['id']} 拒绝但没给原因码"


def test_quoted_danger_word_is_allowed():
    """字符串里出现危险词不能算危险。这是子串黑名单最容易误伤的地方。"""
    for cmd in ('echo "rm -rf /"', "grep -r 'rm -rf' .", "echo 'shutdown'"):
        assert judge(cmd, ROOT).allowed, cmd


def test_broken_quoting_still_catches_danger():
    """引号不配对时 shlex 会失败。退路是按空白硬切，危险命令照样要拦住。"""
    v = judge('rm -rf / "unclosed', ROOT)
    assert v.denied


def test_broken_quoting_does_not_block_harmless():
    """但普通命令不能因为分词失败就被拒 —— 实测这样会白白浪费步数。"""
    assert judge('echo "unclosed', ROOT).allowed
    assert judge('python -c "print(1)', ROOT).allowed


# ---- 文件工具的路径判定 ----

def test_file_write_outside_workspace_denied(tmp_path):
    v = judge_tool_call("write_file", {"path": "../evil.py", "content": ""}, tmp_path)
    assert v is not None and v.denied
    assert v.reason == "outside_workspace"


def test_cannot_write_test_file(tmp_path):
    """改测试等于自己给自己判卷，必须拦住。"""
    v = judge_tool_call("write_file", {"path": "tests/test_a.py", "content": ""}, tmp_path)
    assert v is not None and v.denied
    assert v.reason == "protected_file"

    v2 = judge_tool_call(
        "edit_file", {"path": "tests/test_a.py", "old_text": "a", "new_text": "b"}, tmp_path)
    assert v2 is not None and v2.denied


def test_hidden_test_name_also_protected(tmp_path):
    v = judge_tool_call("write_file", {"path": "hidden_counter.py", "content": ""}, tmp_path)
    assert v is not None and v.denied


def test_can_read_test_file(tmp_path):
    """读测试文件没问题，拦的是改。"""
    v = judge_tool_call("read_file", {"path": "tests/test_a.py"}, tmp_path)
    assert v is None


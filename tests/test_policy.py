"""权限判定的测试。

用例从 eval/permission_cases.json 读 —— 和对照实验共用同一份数据。
两边各写一套的话，慢慢就会漂移，最后实验里的结论和测试里守的东西不是一回事。
"""

import json
from pathlib import Path

from minicode.policy import judge

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


def test_unbalanced_quote_is_denied():
    v = judge('echo "unclosed', ROOT)
    assert v.denied
    assert v.reason == "unparsable"

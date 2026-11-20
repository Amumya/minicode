"""bash 工具的输出上限（流式读取）。

之前的实现是先 `communicate()` 把全部输出读进内存，再截断 —— 截断发生在
内存已经吃满之后：`yes`、递归列大目录这类命令能把服务打爆。
现在改成边读边攒，攒满上限就杀进程，剩下的读掉但丢弃。

这几个测试的价值在于：**证明超量输出不会挂住、也不会无限占内存**。
"""

from __future__ import annotations

import time
from pathlib import Path

from minicode.tools.bash import BashTool, MAX_BYTES


def test_normal_command_output(tmp_path):
    result = BashTool(timeout=20).run({"command": "echo hello"}, tmp_path)
    assert result.exit_code == 0
    assert "hello" in result.content
    assert result.meta.get("truncated") is False


def test_huge_output_is_capped_and_fast(tmp_path):
    """一次性打印远超上限的内容：要快、要标截断、内容不能超过上限太多。"""
    started = time.time()
    result = BashTool(timeout=20).run(
        {"command": "python -c \"print('x' * 500000)\""}, tmp_path
    )
    elapsed = time.time() - started

    assert result.meta.get("truncated") is True, "应该标记为截断"
    assert len(result.content.encode("utf-8")) < MAX_BYTES * 3, "输出不该无上限增长"
    assert elapsed < 15, f"超量输出不该拖很久（实际 {elapsed:.1f}s）"


def test_endless_output_is_killed(tmp_path):
    """无限输出（模拟 `yes`）：必须被杀掉，不能一直跑到超时。"""
    started = time.time()
    result = BashTool(timeout=30).run(
        {"command": "python -c \"import sys\nwhile True: sys.stdout.write('y'*1000)\""},
        tmp_path,
    )
    elapsed = time.time() - started
    assert result.meta.get("truncated") is True
    assert elapsed < 20, f"无限输出应该被提前终止（实际 {elapsed:.1f}s）"


def test_timeout_still_works(tmp_path):
    started = time.time()
    result = BashTool(timeout=1).run(
        {"command": "python -c \"import time; time.sleep(30)\""}, tmp_path
    )
    elapsed = time.time() - started
    assert result.exit_code == 124
    assert result.meta.get("timeout") is True
    assert elapsed < 15, f"超时命令要立刻被杀（实际 {elapsed:.1f}s）"


def test_stderr_is_captured(tmp_path):
    result = BashTool(timeout=20).run(
        {"command": "python -c \"import sys; sys.stderr.write('oops')\""}, tmp_path
    )
    assert "oops" in result.content
    assert "[stderr]" in result.content


def test_missing_command_reports_exit_code(tmp_path):
    result = BashTool(timeout=20).run(
        {"command": "definitely-not-a-real-command-xyz"}, tmp_path
    )
    assert result.exit_code != 0
    assert result.failed is True


def test_empty_command_rejected(tmp_path):
    result = BashTool().run({"command": "   "}, tmp_path)
    assert result.exit_code == 2
    assert result.failed is True

"""bash 工具的测试。不需要 API key，但会真的起子进程。"""

from pathlib import Path

from minicode.tools.bash import BashTool


def test_echo_succeeds(tmp_path):
    r = BashTool().run({"command": "echo hello"}, tmp_path)
    assert r.exit_code == 0
    assert "hello" in r.content
    assert "[exit=0]" in r.to_model_content()


def test_failure_keeps_exit_code(tmp_path):
    """失败必须留下退出码 —— 上一版丢掉退出码，失败就彻底看不见了。"""
    r = BashTool().run({"command": "exit 3"}, tmp_path)
    assert r.exit_code == 3
    assert r.failed
    assert "[exit=3]" in r.to_model_content()


def test_stderr_is_captured(tmp_path):
    cmd = "Write-Error 'boom'" if __import__("os").name == "nt" else "echo boom 1>&2"
    r = BashTool().run({"command": cmd}, tmp_path)
    assert "boom" in r.content


def test_runs_in_workspace(tmp_path):
    (tmp_path / "marker.txt").write_text("x", encoding="utf-8")
    cmd = "Get-ChildItem marker.txt" if __import__("os").name == "nt" else "ls marker.txt"
    r = BashTool().run({"command": cmd}, tmp_path)
    assert r.exit_code == 0
    assert "marker.txt" in r.content


def test_empty_command_rejected(tmp_path):
    r = BashTool().run({}, tmp_path)
    assert r.exit_code is None
    assert "command" in r.content


def test_timeout_kills_and_returns_fast(tmp_path):
    """超时必须真的退出来，而且要把子进程树一起杀掉。

    实测过不杀子进程树的后果：PowerShell 拉起的 python 变成孤儿，
    握着管道不放，整个评测卡死在一个任务上十几分钟。
    """
    import time
    cmd = "Start-Sleep -Seconds 30" if __import__("os").name == "nt" else "sleep 30"
    started = time.time()
    r = BashTool(timeout=1).run({"command": cmd}, tmp_path)
    cost = time.time() - started
    assert r.exit_code == 124
    assert r.meta.get("timeout") is True
    assert cost < 15, f"超时处理太慢: {cost:.1f}s"

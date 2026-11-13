"""执行 shell 命令的工具。

两个决定：

1. Windows 上用 PowerShell 当 shell，不用 cmd。
   模型见过的语料大多是 Unix 写法（ls / cat / grep），cmd 里跑不了。
   PowerShell 自带 ls、cat 这些别名，能少掉一批"命令不存在"的失败。
   代价是 Windows 专有语法和 Unix 语法混着也可能出问题，所以失败率还是要测。

2. 超时和输出截断都在这一层做。
   模型一次调用的输出可能几万字，全塞回上下文会直接把窗口撑满。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from minicode.tools.base import ToolResult

DEFAULT_TIMEOUT = 60
MAX_BYTES = 60_000


def _wrap(command: str) -> list[str]:
    if os.name == "nt":
        return ["powershell", "-NoProfile", "-NonInteractive", "-Command", command]
    return ["/bin/sh", "-c", command]


class BashTool:
    name = "bash"
    description = (
        "在工作目录里执行一条 shell 命令，返回输出和退出码。"
        "Windows 环境下用的是 PowerShell。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "要执行的命令"}
        },
        "required": ["command"],
    }

    def __init__(self, timeout: int = DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    def run(self, args: dict, cwd: Path) -> ToolResult:
        command = (args or {}).get("command", "")
        if not isinstance(command, str) or not command.strip():
            return ToolResult("没有拿到 command 参数", exit_code=None)

        popen_kwargs: dict = {
            "cwd": str(cwd),
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
        }
        if os.name != "nt":
            # 单独开一个进程组，超时的时候能把孙进程一起收拾掉
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(_wrap(command), **popen_kwargs)
        except FileNotFoundError as exc:
            return ToolResult(f"启动 shell 失败: {exc}", exit_code=127)

        try:
            raw_out, raw_err = proc.communicate(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            try:
                raw_out, raw_err = proc.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                raw_out, raw_err = b"", b""
            return ToolResult(
                f"命令超时（{self.timeout}s），已连同子进程一起杀掉",
                exit_code=124,
                meta={"timeout": True, "command": command},
            )

        out = _decode(raw_out)
        err = _decode(raw_err)
        body = out
        if err:
            body = f"{out}\n[stderr]\n{err}" if out else f"[stderr]\n{err}"

        truncated = len(body.encode("utf-8", "replace")) > MAX_BYTES
        if truncated:
            body = body.encode("utf-8", "replace")[:MAX_BYTES].decode("utf-8", "replace")

        return ToolResult(
            body, exit_code=proc.returncode,
            meta={"command": command, "truncated": truncated, "shell": sys.platform},
        )


def _kill_tree(proc: subprocess.Popen) -> None:
    """把子进程连同它拉起来的进程一起杀掉。

    只 kill 直接子进程不够：实测 PowerShell 拉起的 python 会变成孤儿继续跑，
    而且它握着 stdout 管道不放，父进程的 communicate() 会一直等下去 ——
    表现就是整个评测卡死在一个任务上十几分钟。
    """
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True, timeout=10,
            )
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    try:
        proc.kill()
    except OSError:
        pass



def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")

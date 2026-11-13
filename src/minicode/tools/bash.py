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

        try:
            proc = subprocess.run(
                _wrap(command),
                cwd=str(cwd),
                capture_output=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(
                f"命令超时（{self.timeout}s）", exit_code=124,
                meta={"timeout": True, "command": command},
            )
        except FileNotFoundError as exc:
            return ToolResult(f"启动 shell 失败: {exc}", exit_code=127)

        out = _decode(proc.stdout)
        err = _decode(proc.stderr)
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


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    for enc in ("utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")

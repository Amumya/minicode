"""系统提示词。

这里的文字会按运行环境生成 —— Windows 下要告诉模型"底层是 PowerShell"，
否则它会按 Unix 习惯写 `wc -l`、`2>/dev/null`，然后一路失败。

判断用 `os.name == "nt"`，和 `tools/bash.py` 里选 shell 用的是同一个判断，
保证"实际用哪个 shell"和"告诉模型用哪个 shell"不会说两套话。
"""

from __future__ import annotations

import os

BASE = (
    "你是一个在命令行里干活的助手。可以用 bash 工具执行命令来完成用户的任务。"
    "命令跑完会带上退出码，退出码非 0 表示失败。任务完成后直接给出结论，不要再调工具。"
)

WINDOWS_HINT = (
    "\n\n注意：当前是 Windows，bash 工具底层是 PowerShell，不是 bash。"
    "请用 PowerShell 能跑的写法：看目录用 Get-ChildItem（ls 也能用），"
    "读文件用 Get-Content（cat 也能用），"
    "不要用 wc、tail、head、grep 的管道组合，也不要用 2>/dev/null 这种重定向。"
    "统计数量用 (Get-ChildItem ...).Count，筛选内容用 Select-String。"
)

POSIX_HINT = "\n\n当前是 Linux/macOS 环境，bash 工具底层是 /bin/sh。"


def base_text() -> str:
    """不带任何环境说明的提示词，测试用。"""
    return BASE


def default_system(shell_hint: bool = True) -> str:
    """拼出系统提示词。

    shell_hint=False 用来做对照实验：同样的任务，一句环境说明不加会怎样。
    """
    if not shell_hint:
        return BASE
    return BASE + (WINDOWS_HINT if os.name == "nt" else POSIX_HINT)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把"工具调用返回非 0"按原因分一下类。

为什么需要这个：
    直接把 `exit_code != 0` 当"工具失败"会把数字弄脏。比如 `pytest` 发现测试不红
    的时候返回 1 —— 那是 agent 需要的信息，不是环境出问题。混在一起算，
    "失败率 29%" 这个数字就没法解释，也没法用来判断改了提示词有没有用。

分类只有四类：
    ok            退出码 0
    test_red      测试没通过（正常信号）
    shell_mismatch 用了该 shell 没有的命令/写法（真正的环境问题）
    other         其它非 0
"""

from __future__ import annotations

import re

TEST_CMD = re.compile(r"\b(pytest|unittest)\b|python\s+-m\s+pytest", re.IGNORECASE)
UNIX_ONLY = re.compile(
    r"(?:^|[\s|;&(])(wc|tail|head|sed|awk|find|chmod|2>/dev/null)\b",
    re.IGNORECASE,
)
UNIX_REDIRECT = re.compile(r"2>\s*/dev/null")


def classify(command: str, exit_code: int | None) -> str:
    if exit_code is None:
        return "ok"
    if exit_code == 0:
        return "ok"
    if exit_code == 124:
        return "timeout"
    cmd = command or ""
    if TEST_CMD.search(cmd):
        return "test_red"
    if UNIX_ONLY.search(cmd) or UNIX_REDIRECT.search(cmd):
        return "shell_mismatch"
    return "other"

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


def default_system(
    shell_hint: bool = True,
    skills=None,
    python_hint: str | None = None,
    plan_hint: bool = False,
) -> str:
    """拼出系统提示词。

    几个参数都是为了"把环境事实告诉 agent"，而不是让它自己试出来：

        shell_hint  False 时用来做对照实验：一句环境说明不加会怎样
        skills      传 SkillLoader 时只拼**目录**（名字 + 一句描述），
                    正文留给 agent 按需 load_skill —— 系统提示词每轮都要发
        python_hint 该用哪个解释器。实测不说的话，agent 会用系统 python 跑
                    pytest，失败后再花好几步去找装了 pytest 的那个
        plan_hint   开了 todo 工具时提示它多步任务先写清单
    """
    text = BASE if not shell_hint else BASE + (WINDOWS_HINT if os.name == "nt" else POSIX_HINT)

    if python_hint:
        text += (
            f"\n\nPython 用这个解释器：{python_hint}"
            "（它里面装了项目依赖；直接用 `python` 可能缺包）。"
        )

    if plan_hint:
        text += (
            "\n\n任务超过两三步时，先用 todo_write 把计划写下来，"
            "每完成一步就更新清单的状态。"
        )

    catalog = ""
    if skills is not None:
        try:
            catalog = skills.catalog()
        except Exception:  # noqa: BLE001 - 技能目录读不出来不该让整轮跑挂
            catalog = ""
    if catalog:
        text += (
            "\n\n可以按需加载的技能（先看目录，真要用再调 load_skill 取全文）：\n"
            + catalog
        )
    return text

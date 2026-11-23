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
    compact_hint: bool = False,
    tasks_hint: bool = False,
    memory_hint: bool = False,
    memory_snapshot: str | None = None,
    mcp_servers: list[str] | None = None,
    subagent_hint: bool = False,
    team_hint: bool = False,
) -> str:
    """拼出系统提示词。

    几个参数都是为了"把环境事实告诉 agent"，而不是让它自己试出来：

        shell_hint  False 时用来做对照实验：一句环境说明不加会怎样
        skills      传 SkillLoader 时只拼**目录**（名字 + 一句描述），
                    正文留给 agent 按需 load_skill —— 系统提示词每轮都要发
        python_hint 该用哪个解释器。实测不说的话，agent 会用系统 python 跑
                    pytest，失败后再花好几步去找装了 pytest 的那个
        plan_hint   开了 todo 工具时提示它多步任务先写清单
        tasks_hint  开了任务图时告诉它"什么情况该用、怎么用" ——
                    只把工具挂上不说用途的话，实测模型一次都不会主动调
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

    # compact 工具同理：不写清「什么时候压、压完会丢什么」，它要么不调，要么在最不该压的时候调。
    if compact_hint:
        text += (
            "\n\n上下文快满、或一个大阶段做完要开始查别的东西时，可以调 compact "
            "把前面的历史整理成摘要。节奏上：手头还有没写完的改动时不要压，"
            "阶段收尾、准备换方向时压最划算。"
        )

    if tasks_hint:
        text += (
            "\n\n有一组任务图工具（create_task / list_tasks / get_task / "
            "claim_task / complete_task / update_task）。"
            "当任务需要多步、且步骤之间有先后关系时，先 create_task 建条目，"
            "用 blockedBy 表达「这一步依赖哪几步」，然后按顺序 claim_task 认领、"
            "做完 complete_task 收尾 —— 完成时会告诉你解锁了哪些任务。"
            "简单的一两步任务直接用 bash 做，不要为了用而用。"
        )

    if subagent_hint:
        text += (
            "\n\n有一个 `task` 工具：把独立的小任务交给子代理，只拿回结论。"
            "适合「要翻很多文件但只想要结论」；子代理中间过程不进你的上下文。"
        )

    if team_hint:
        text += (
            "\n\n有一个 `team` 工具：给一个总任务和 2–6 个带角色的成员，"
            "成员并行工作、各自返回结论。适合多角度审查、并行调查或互相独立的子任务。"
        )

    if mcp_servers:
        names = "、".join(mcp_servers)
        text += (
            f"\n\n另外挂了外部 MCP 工具（来自 server：{names}），工具名以 `mcp__` 开头。"
            "它们是独立进程提供的，参数和副作用对我们不透明，只能按前面对外暴露的白名单用。"
            "调用前先想清楚参数；不确定的先用最小参数试。"
        )

    if memory_hint:
        text += (
            "\n\n有一组长期记忆工具（remember / recall / forget）。"
            "遇到以后还用得上的事实——项目约定、环境事实、用户偏好、踩过的坑——"
            "就用 remember 记一条，一句话说具体；"
            "不要记密码、密钥、一次性的中间结果和大段代码。"
            "系统提示词里只会带上和当前任务最相关的一小部分记忆，"
            "需要更多上下文时用 recall 查。"
        )

    if memory_snapshot:
        text += "\n\n已有的长期记忆（和这次任务相关的部分）：\n" + memory_snapshot

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

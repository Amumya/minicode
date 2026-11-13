"""把技能做成工具：agent 需要的时候自己去取全文。

`load_skill` 不带参数时返回目录，带名字时返回那一份的正文。
目录在系统提示词里已经有了，这里再提供一次是为了让 agent 能刷新
（万一它把目录忘了）。
"""

from __future__ import annotations

from pathlib import Path

from minicode.skills import SkillLoader
from minicode.tools.base import ToolResult


class LoadSkillTool:
    name = "load_skill"
    description = (
        "取一份技能的完整说明。不带参数时列出所有技能，"
        "带 name 时返回那一份的正文。只在确实要按某套流程做事时才调。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "技能名，省略则只列目录"}
        },
    }

    def __init__(self, loader: SkillLoader) -> None:
        self.loader = loader

    def run(self, args: dict, cwd: Path) -> ToolResult:
        name = str((args or {}).get("name", "")).strip()
        if not name:
            catalog = self.loader.catalog()
            if not catalog:
                return ToolResult("没有可用的技能", exit_code=0)
            return ToolResult(f"可用技能：\n{catalog}", exit_code=0)

        skill = self.loader.get(name)
        if skill is None:
            available = ", ".join(self.loader.names()) or "（无）"
            return ToolResult(f"没有叫 {name} 的技能。可用：{available}", exit_code=1)
        return ToolResult(skill.body, exit_code=0,
                          meta={"skill": name, "chars": len(skill.body)})

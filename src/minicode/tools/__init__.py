"""工具集合。

`build_registry` 是拼装入口。参数是"要装哪些能力"，默认一个都不多给 ——
这样消融实验里"加不加某个工具"才是一个干净的变量。

`default_registry` 保留给老调用方，行为等价于 `build_registry(files=...)`。
"""

from __future__ import annotations

from minicode.skills import SkillLoader
from minicode.tools.base import Registry, ToolResult
from minicode.tools.bash import BashTool
from minicode.tools.files import EditFileTool, ReadFileTool, WriteFileTool
from minicode.tools.skills import LoadSkillTool
from minicode.tools.todo import TodoList, TodoWriteTool

__all__ = [
    "Registry",
    "ToolResult",
    "BashTool",
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "TodoList",
    "TodoWriteTool",
    "LoadSkillTool",
    "build_registry",
    "default_registry",
]


def build_registry(
    *,
    files: bool = False,
    todo: TodoList | None = None,
    skills: SkillLoader | None = None,
    extra: list | None = None,
) -> Registry:
    reg = Registry()
    reg.add(BashTool())
    if files:
        reg.add(ReadFileTool())
        reg.add(WriteFileTool())
        reg.add(EditFileTool())
    if todo is not None:
        reg.add(TodoWriteTool(todo))
    if skills is not None:
        reg.add(LoadSkillTool(skills))
    for tool in extra or []:
        reg.add(tool)
    return reg


def default_registry(include_files: bool = False) -> Registry:
    return build_registry(files=include_files)

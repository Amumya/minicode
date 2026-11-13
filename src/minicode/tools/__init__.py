"""工具集合。"""

from __future__ import annotations

from minicode.tools.base import Registry, ToolResult
from minicode.tools.bash import BashTool

__all__ = ["Registry", "ToolResult", "BashTool", "default_registry"]


def default_registry() -> Registry:
    reg = Registry()
    reg.add(BashTool())
    return reg

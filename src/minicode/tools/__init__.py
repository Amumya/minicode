"""工具集合。"""

from __future__ import annotations

from minicode.tools.base import Registry, ToolResult
from minicode.tools.bash import BashTool
from minicode.tools.files import EditFileTool, ReadFileTool, WriteFileTool

__all__ = [
    "Registry",
    "ToolResult",
    "BashTool",
    "ReadFileTool",
    "WriteFileTool",
    "EditFileTool",
    "default_registry",
]


def default_registry(include_files: bool = False) -> Registry:
    """默认给 bash。include_files=True 时加上文件读写。

    默认不带文件工具，是因为要做消融实验：条件 A 只有 bash，条件 B 加文件工具，
    这样"多出来的那点通过率"归因才干净。
    """
    reg = Registry()
    reg.add(BashTool())
    if include_files:
        reg.add(ReadFileTool())
        reg.add(WriteFileTool())
        reg.add(EditFileTool())
    return reg

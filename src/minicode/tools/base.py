"""工具的基础类型。

一个决定：工具的返回值里**必须带退出码**。
上一版实测过，把 stdout 和 stderr 拼成一段文本丢掉退出码，会让失败彻底看不见 ——
模型不知道命令错了，评测也算不出失败率。所以这里把退出码显式写进给模型看的内容里。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

MAX_MODEL_CHARS = 8000


@dataclass
class ToolResult:
    content: str
    exit_code: int | None = None
    meta: dict = field(default_factory=dict)

    @property
    def failed(self) -> bool:
        return self.exit_code is not None and self.exit_code != 0

    def to_model_content(self) -> str:
        text = self.content
        if len(text) > MAX_MODEL_CHARS:
            text = text[:MAX_MODEL_CHARS] + f"\n…（已截断，原长 {len(self.content)}）"
        if self.exit_code is None:
            return text
        return f"[exit={self.exit_code}]\n{text}"


class Tool(Protocol):
    name: str
    description: str
    input_schema: dict

    def run(self, args: dict, cwd: Path) -> ToolResult: ...


class Registry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> Tool:
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def schemas(self) -> list[dict]:
        return [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema,
            }
            for t in self._tools.values()
        ]

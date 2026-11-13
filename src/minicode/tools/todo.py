"""任务清单工具。

为什么要有它：多步任务里模型容易跑偏 —— 做着做着忘了原目标是哪几条。
让它把计划写下来、每步更新，比在提示词里要求它"记得分步骤"更可靠。

几条约束（都是刻意的）：
    * 状态只能是 pending / in_progress / completed
    * **同一时间最多一条 in_progress** —— 同时干三件事通常等于三件都没干完
    * 条目数上限 30，防止把清单当聊天记录写
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from minicode.tools.base import ToolResult

STATUSES = ("pending", "in_progress", "completed")
MAX_ITEMS = 30
_MARK = {"pending": " ", "in_progress": "~", "completed": "x"}


@dataclass(frozen=True)
class TodoItem:
    content: str
    status: str = "pending"


class TodoList:
    def __init__(self) -> None:
        self.items: list[TodoItem] = []

    def clear(self) -> None:
        self.items = []

    def update(self, raw: object) -> tuple[bool, str]:
        """校验并替换整个清单。返回 (是否成功, 给模型看的文本)。"""
        if not isinstance(raw, list):
            return False, "todos 必须是一个数组"
        if len(raw) > MAX_ITEMS:
            return False, f"条目太多（{len(raw)}），上限 {MAX_ITEMS}"

        items: list[TodoItem] = []
        for i, entry in enumerate(raw):
            if not isinstance(entry, dict):
                return False, f"第 {i + 1} 条不是对象"
            content = str(entry.get("content", "")).strip()
            status = str(entry.get("status", "pending")).strip() or "pending"
            if not content:
                return False, f"第 {i + 1} 条没有 content"
            if status not in STATUSES:
                return False, f"第 {i + 1} 条状态非法: {status}（只能是 {'/'.join(STATUSES)}）"
            items.append(TodoItem(content=content, status=status))

        doing = [i for i, it in enumerate(items) if it.status == "in_progress"]
        if len(doing) > 1:
            return False, f"同时有 {len(doing)} 条 in_progress，最多只能一条"

        self.items = items
        return True, self.render()

    def render(self) -> str:
        if not self.items:
            return "（清单是空的）"
        lines = []
        for it in self.items:
            lines.append(f"- [{_MARK[it.status]}] {it.content}")
        done = sum(1 for it in self.items if it.status == "completed")
        lines.append(f"\n进度：{done}/{len(self.items)} 完成")
        return "\n".join(lines)

    def snapshot(self) -> list[dict]:
        return [{"content": it.content, "status": it.status} for it in self.items]


class TodoWriteTool:
    name = "todo_write"
    description = (
        "写下或更新任务清单。传完整的清单（会整体替换），"
        "状态用 pending / in_progress / completed，同一时间最多一条 in_progress。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {"type": "string",
                                   "enum": list(STATUSES)},
                    },
                    "required": ["content"],
                },
            }
        },
        "required": ["todos"],
    }

    def __init__(self, todo: TodoList | None = None) -> None:
        self.todo = todo or TodoList()

    def run(self, args: dict, cwd: Path) -> ToolResult:
        ok, text = self.todo.update((args or {}).get("todos"))
        return ToolResult(text, exit_code=0 if ok else 1,
                          meta={"items": len(self.todo.items), "ok": ok})

"""记忆相关工具：记一条、查一条、删一条。

为什么工具而不是让模型直接写文件：
    * 走工具才能控制格式（JSONL、去重、上限），文件格式不会因为模型发挥而变形
    * 工具调用会被事件流记录下来，界面上能看见"这次记住了什么"
"""

from __future__ import annotations

from pathlib import Path

from minicode.memory import MemoryStore
from minicode.tools.base import ToolResult


class RememberTool:
    name = "remember"
    description = (
        "把一条以后还用得上的事实记进长期记忆（跨 run 生效）。"
        "适合记：项目约定、环境事实、用户偏好、踩过的坑。"
        "不要记：密码/密钥、一次性的中间结果、大段代码。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "要记住的一句话，尽量具体"},
            "tags": {
                "type": "string",
                "description": "可选标签，逗号分隔，例如：环境,pytest",
            },
        },
        "required": ["text"],
    }

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def run(self, args: dict, cwd: Path) -> ToolResult:
        text = str((args or {}).get("text", "")).strip()
        if not text:
            return ToolResult("没有拿到 text 参数", exit_code=None)
        tags_raw = str((args or {}).get("tags", "") or "")
        tags = [t.strip() for t in tags_raw.replace("，", ",").split(",") if t.strip()]
        try:
            entry, created = self.store.add(text, tags)
        except (OSError, ValueError) as exc:
            return ToolResult(f"写入记忆失败：{exc}", exit_code=1)
        total = len(self.store.entries())
        verb = "已记住" if created else "这条记过了（未重复写入）"
        return ToolResult(
            f"{verb}：{entry.id}（当前共 {total} 条）",
            exit_code=0,
            meta={"memory_id": entry.id, "created": created, "total": total},
        )


class RecallTool:
    name = "recall"
    description = (
        "按关键词查长期记忆。系统提示词里只带了最相关的一小部分，"
        "需要更多上下文时用它。"
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词，空则返回最近的几条"},
            "limit": {"type": "integer", "description": "最多返回几条，默认 8"},
        },
        "required": ["query"],
    }

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def run(self, args: dict, cwd: Path) -> ToolResult:
        query = str((args or {}).get("query", "") or "")
        try:
            limit = int((args or {}).get("limit") or 8)
        except (TypeError, ValueError):
            limit = 8
        picked = self.store.relevant(query, limit=max(1, min(limit, 50)))
        if not picked:
            return ToolResult("没有相关记忆", exit_code=0, meta={"count": 0})
        return ToolResult(
            "\n".join(entry.line() for entry in picked),
            exit_code=0,
            meta={"count": len(picked)},
        )


class ForgetTool:
    name = "forget"
    description = "删掉一条记错的长期记忆（需要 id，id 在 remember 的返回里）。"
    input_schema = {
        "type": "object",
        "properties": {
            "id": {"type": "string", "description": "要删除的记忆 id"},
        },
        "required": ["id"],
    }

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    def run(self, args: dict, cwd: Path) -> ToolResult:
        entry_id = str((args or {}).get("id", "")).strip()
        if not entry_id:
            return ToolResult("没有拿到 id 参数", exit_code=None)
        try:
            removed = self.store.forget(entry_id)
        except OSError as exc:
            return ToolResult(f"删除失败：{exc}", exit_code=1)
        if not removed:
            return ToolResult(f"没有找到 id={entry_id} 的记忆", exit_code=1)
        return ToolResult(f"已删除 {entry_id}", exit_code=0, meta={"memory_id": entry_id})


def memory_tools(store: MemoryStore) -> list:
    return [RememberTool(store), RecallTool(store), ForgetTool(store)]

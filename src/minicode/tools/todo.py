"""任务清单工具。

为什么要有它：多步任务里模型容易跑偏 —— 做着做着忘了原目标是哪几条。
让它把计划写下来、每步更新，比在提示词里要求它"记得分步骤"更可靠。

几条约束（都是刻意的）：
    * 状态只能是 pending / in_progress / completed
    * **同一时间最多一条 in_progress** —— 同时干三件事通常等于三件都没干完
    * 条目数上限 30，防止把清单当聊天记录写
    * **可以声明依赖**（blockedBy，1 基编号），并且提交时会做**环检测**：
      有环的计划等于永远开不了工，而且从界面上看不出来哪里错了
    * 依赖没完成的任务不许标 in_progress / completed

为什么不做成"每条一个 ID + 增量增删改认领"（参考项目 s10 的 task_system）：
    那是为多代理抢活设计的。这里只有一个 agent、一次 run，
    整份替换比增量操作更不容易出错，而且省下五个工具的 schema 成本。
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
    blocked_by: tuple[int, ...] = ()      # 1 基编号


class TodoList:
    def __init__(self) -> None:
        self.items: list[TodoItem] = []

    def clear(self) -> None:
        self.items = []

    # ---------------------------------------------------------------- 校验

    @staticmethod
    def _parse_blockers(raw: object, index: int) -> tuple[bool, tuple[int, ...], str]:
        """解析一条的 blockedBy。返回 (是否合法, 依赖元组, 错误信息)。"""
        if raw is None:
            return True, (), ""
        if not isinstance(raw, list):
            return False, (), f"第 {index} 条的 blockedBy 必须是数组"
        out: list[int] = []
        for v in raw:
            if isinstance(v, bool) or not isinstance(v, int):
                return False, (), f"第 {index} 条的 blockedBy 里有个不是整数：{v!r}"
            if v == index:
                return False, (), f"第 {index} 条不能依赖自己"
            out.append(v)
        return True, tuple(out), ""

    @staticmethod
    def _find_cycle(items: list[TodoItem]) -> list[int] | None:
        """在有向图上找环。返回环上的编号（1 基），没环返回 None。

        为什么必须查：环不会报错，只会让计划永远开不了工 ——
        A 等 B、B 等 A，两条都停在 pending，而模型会觉得"我先做别的"。
        这种错在日志里几乎看不出来，所以要在提交那一刻拦下。
        """
        n = len(items)
        state = [0] * (n + 1)          # 0 未访问 / 1 在栈上 / 2 已完成
        stack: list[int] = []

        def visit(node: int) -> list[int] | None:
            if state[node] == 1:       # 回到栈上的点 = 有环
                if node in stack:
                    return stack[stack.index(node):] + [node]
                return [node, node]
            if state[node] == 2:
                return None
            state[node] = 1
            stack.append(node)
            for dep in items[node - 1].blocked_by:
                found = visit(dep)
                if found:
                    return found
            stack.pop()
            state[node] = 2
            return None

        for i in range(1, n + 1):
            if state[i] == 0:
                found = visit(i)
                if found:
                    return found
        return None

    def update(self, raw: object) -> tuple[bool, str]:
        """校验并替换整个清单。返回 (是否成功, 给模型看的文本)。

        失败时**不改动已有清单** —— 模型写错一次不该把计划弄丢。
        """
        if not isinstance(raw, list):
            return False, "todos 必须是一个数组"
        if len(raw) > MAX_ITEMS:
            return False, f"条目太多（{len(raw)}），上限 {MAX_ITEMS}"

        items: list[TodoItem] = []
        for i, entry in enumerate(raw, start=1):
            if not isinstance(entry, dict):
                return False, f"第 {i} 条不是对象"
            content = str(entry.get("content", "")).strip()
            status = str(entry.get("status", "pending")).strip() or "pending"
            if not content:
                return False, f"第 {i} 条没有 content"
            if status not in STATUSES:
                return False, f"第 {i} 条状态非法: {status}（只能是 {'/'.join(STATUSES)}）"
            ok, blockers, err = self._parse_blockers(entry.get("blockedBy"), i)
            if not ok:
                return False, err
            items.append(TodoItem(content=content, status=status, blocked_by=blockers))

        # 依赖编号必须在范围内
        for i, it in enumerate(items, start=1):
            for dep in it.blocked_by:
                if dep < 1 or dep > len(items):
                    return False, f"第 {i} 条依赖了不存在的编号 {dep}（共 {len(items)} 条）"

        cycle = self._find_cycle(items)
        if cycle:
            return False, f"计划里有环：{' → '.join(str(x) for x in cycle)}（永远不会开工）"

        doing = [i for i, it in enumerate(items, start=1) if it.status == "in_progress"]
        if len(doing) > 1:
            return False, f"同时有 {len(doing)} 条 in_progress（第 {doing} 条），最多只能一条"

        # 依赖没完成的，不许标 in_progress / completed
        for i, it in enumerate(items, start=1):
            if it.status == "pending":
                continue
            unfinished = [d for d in it.blocked_by if items[d - 1].status != "completed"]
            if unfinished:
                return False, (
                    f"第 {i} 条还有未完成的前置 {unfinished}，不能标成 {it.status}"
                )

        self.items = items
        return True, self.render()

    def render(self) -> str:
        if not self.items:
            return "（清单是空的）"
        lines = []
        for i, it in enumerate(self.items, start=1):
            line = f"{i}. [{_MARK[it.status]}] {it.content}"
            if it.blocked_by:
                waiting = [d for d in it.blocked_by
                           if self.items[d - 1].status != "completed"]
                if waiting:
                    line += f"  ← 等 {waiting}"
            lines.append(line)
        done = sum(1 for it in self.items if it.status == "completed")
        lines.append(f"\n进度：{done}/{len(self.items)} 完成")
        return "\n".join(lines)

    def snapshot(self) -> list[dict]:
        return [
            {"content": it.content, "status": it.status,
             "blockedBy": list(it.blocked_by)}
            for it in self.items
        ]


class TodoWriteTool:
    name = "todo_write"
    description = (
        "写下或更新任务清单。传完整清单（整体替换），编号从 1 开始。"
        "状态用 pending / in_progress / completed，同一时间最多一条 in_progress。"
        "有先后关系时用 blockedBy 填前置条目的编号；前置没完成的不许标成进行中或已完成。"
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
                        "status": {"type": "string", "enum": list(STATUSES)},
                        "blockedBy": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": "前置条目的编号（1 基），全部完成后本条才能开工",
                        },
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

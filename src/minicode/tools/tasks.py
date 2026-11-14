"""把任务图包装成 6 个工具。

为什么是 6 个而不是 1 个：任务图的操作是**增量**的 ——
新建一条、加个依赖、认领、完成。整份替换在有依赖和归属的情况下说不清
"这条是不是刚才那条"。

代价是 schema 变贵（约 +800 token/轮），所以这 6 个工具**默认不挂**，
要 `--tasks` 才开 —— 和 `--files` 一个思路，保证消融实验里它是个干净变量。
"""

from __future__ import annotations

import json
from pathlib import Path

from minicode.tasks import TaskStore
from minicode.tools.base import ToolResult


def _err(msg: str) -> ToolResult:
    return ToolResult(msg, exit_code=1)


class _Base:
    def __init__(self, store: TaskStore) -> None:
        self.store = store


class CreateTaskTool(_Base):
    name = "create_task"
    description = "新建一条待办任务，返回它的 ID。有前置任务时用 blockedBy 填它们的 ID。"
    input_schema = {
        "type": "object",
        "properties": {
            "subject": {"type": "string", "description": "一句话说清这条要做什么"},
            "description": {"type": "string", "description": "补充说明（可选）"},
            "blockedBy": {
                "type": "array",
                "items": {"type": "string"},
                "description": "前置任务的 ID 列表，全部完成后本条才能开工",
            },
        },
        "required": ["subject"],
    }

    def run(self, args: dict, cwd: Path) -> ToolResult:
        try:
            task = self.store.create(
                str((args or {}).get("subject", "")),
                str((args or {}).get("description", "")),
                list((args or {}).get("blockedBy") or []),
            )
        except ValueError as exc:
            return _err(str(exc))
        return ToolResult(f"已创建 {task.id}: {task.subject}", exit_code=0,
                          meta={"id": task.id})


class UpdateTaskTool(_Base):
    name = "update_task"
    description = "给一条已有任务追加前置依赖（只加不改，成环会被拒绝）。"
    input_schema = {
        "type": "object",
        "properties": {
            "taskId": {"type": "string"},
            "addBlockedBy": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["taskId", "addBlockedBy"],
    }

    def run(self, args: dict, cwd: Path) -> ToolResult:
        task_id = str((args or {}).get("taskId", "")).strip()
        add = [str(x) for x in ((args or {}).get("addBlockedBy") or [])]
        if not task_id:
            return _err("缺少 taskId")
        try:
            task = self.store.update_dependencies(task_id, add)
        except (ValueError, FileNotFoundError) as exc:
            return _err(str(exc))
        return ToolResult(f"{task.id} 的前置现在是 {task.blocked_by}", exit_code=0)


class ListTasksTool(_Base):
    name = "list_tasks"
    description = "列出所有任务和它们的进度。"
    input_schema = {"type": "object", "properties": {}}

    def run(self, args: dict, cwd: Path) -> ToolResult:
        return ToolResult(self.store.render_list(), exit_code=0)


class GetTaskTool(_Base):
    name = "get_task"
    description = "看一条任务的完整信息（含前置和认领者）。"
    input_schema = {
        "type": "object",
        "properties": {"taskId": {"type": "string"}},
        "required": ["taskId"],
    }

    def run(self, args: dict, cwd: Path) -> ToolResult:
        task_id = str((args or {}).get("taskId", "")).strip()
        try:
            task = self.store.load(task_id)
        except (ValueError, FileNotFoundError) as exc:
            return _err(str(exc))
        blocked = self.store.incomplete_dependencies(task)
        detail = task.to_public()
        detail["unfinishedDependencies"] = blocked
        return ToolResult(json.dumps(detail, ensure_ascii=False, indent=2), exit_code=0)


class ClaimTaskTool(_Base):
    name = "claim_task"
    description = "认领一条任务（pending → in_progress）。前置没做完或已被认领会失败。"
    input_schema = {
        "type": "object",
        "properties": {
            "taskId": {"type": "string"},
            "owner": {"type": "string", "description": "谁在做，默认 agent"},
        },
        "required": ["taskId"],
    }

    def run(self, args: dict, cwd: Path) -> ToolResult:
        task_id = str((args or {}).get("taskId", "")).strip()
        owner = str((args or {}).get("owner", "agent")).strip() or "agent"
        try:
            task = self.store.claim(task_id, owner)
        except (ValueError, FileNotFoundError) as exc:
            return _err(str(exc))
        return ToolResult(f"已认领 {task.id}（owner: {task.owner}）", exit_code=0)


class CompleteTaskTool(_Base):
    name = "complete_task"
    description = "完成一条已认领的任务，并告诉你因此解锁了哪些任务。"
    input_schema = {
        "type": "object",
        "properties": {
            "taskId": {"type": "string"},
            "owner": {"type": "string", "description": "谁完成的，默认不校验"},
        },
        "required": ["taskId"],
    }

    def run(self, args: dict, cwd: Path) -> ToolResult:
        task_id = str((args or {}).get("taskId", "")).strip()
        owner = args.get("owner")
        try:
            task, unlocked = self.store.complete(task_id, owner)
        except (ValueError, FileNotFoundError) as exc:
            return _err(str(exc))
        text = f"已完成 {task.id}（{task.subject}）"
        if unlocked:
            text += f"\n现在可以做：{', '.join(unlocked)}"
        return ToolResult(text, exit_code=0, meta={"unlocked": unlocked})


def task_tools(store: TaskStore) -> list:
    return [
        CreateTaskTool(store),
        UpdateTaskTool(store),
        ListTasksTool(store),
        GetTaskTool(store),
        ClaimTaskTool(store),
        CompleteTaskTool(store),
    ]


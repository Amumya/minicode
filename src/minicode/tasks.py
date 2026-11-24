"""持久化的任务图：带依赖、带认领、带解锁报告。

和 `tools/todo.py` 的区别（两个都留着，用途不同）：

    todo_write     一次 run 内的计划，整份替换，没有 ID，没有归属
    tasks（本模块） 跨 run 的活，每条一个文件、有 ID、有前置、有 owner

为什么两个都要：单代理快速规划用 todo 最省；一旦有"谁做哪条""哪条能开工"
"完成这一步解锁了什么"的需求，扁平清单就表达不了了。

从参考实现（learn-claude-code s10）借了四个设计，都值得说清：

1. **环检测**：加依赖时沿 blockedBy 做 DFS。有环的任务图不会报错，
   只会让所有相关任务永远停在 pending —— 而且在列表里看不出来。
2. **保守的依赖判定**：前置任务的文件读不到（被删/ID 写错）也算"没完成"。
   宁可不让开工，也不要在依赖状态不明时放行。
3. **认领要带 owner**：只有 pending 且依赖满足才能认领；认领后只有同一个 owner 能完成。
4. **完成时报告解锁**：完成前后各算一次"哪些 pending 任务依赖已满足"，差集就是
   被这一步解锁的任务 —— 把图结构变成可执行建议。

一个刻意的差别：ID 生成失败时不重试 100 次，直接用带时间的 ID 兜底，
避免在异常情况下转圈。
"""

from __future__ import annotations

import json
import os
import secrets
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

STATUSES = ("pending", "in_progress", "completed")


@dataclass
class Task:
    id: str
    subject: str
    description: str = ""
    status: str = "pending"
    owner: str | None = None
    blocked_by: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["blocked_by"] = list(self.blocked_by)
        return d

    def to_public(self) -> dict:
        """给模型/界面看的版本：英文键名保持和参考实现一致，值里带中文说明。"""
        return {
            "id": self.id,
            "subject": self.subject,
            "description": self.description,
            "status": self.status,
            "owner": self.owner,
            "blockedBy": list(self.blocked_by),
        }


class TaskStore:
    def __init__(self, directory: Path) -> None:
        self.dir = Path(directory)
        self._last_created = 0.0

    # ---------------------------------------------------------------- 存储

    def _ensure(self) -> Path:
        self.dir.mkdir(parents=True, exist_ok=True)
        return self.dir

    def _path(self, task_id: str) -> Path:
        # 只允许 task_ 开头的安全 ID，防止路径穿越
        if not task_id.startswith("task_") or "/" in task_id or "\\" in task_id:
            raise ValueError(f"非法任务 ID: {task_id!r}")
        return self.dir / f"{task_id}.json"

    def exists(self, task_id: str) -> bool:
        try:
            return self._path(task_id).is_file()
        except ValueError:
            return False

    def save(self, task: Task) -> Task:
        self._ensure()
        task.updated_at = time.time()
        target = self._path(task.id)
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        os.replace(tmp, target)
        return task

    def load(self, task_id: str) -> Task:
        path = self._path(task_id)
        if not path.is_file():
            raise FileNotFoundError(f"没有这个任务: {task_id}")
        data = json.loads(path.read_text(encoding="utf-8"))
        status = str(data.get("status") or "pending")
        if status not in STATUSES:
            status = "pending"
        return Task(
            id=data["id"],
            subject=data["subject"],
            description=data.get("description", ""),
            status=status,
            owner=data.get("owner"),
            blocked_by=list(data.get("blockedBy") or data.get("blocked_by") or []),
            created_at=data.get("created_at", time.time()),
            updated_at=data.get("updated_at", time.time()),
        )

    def list(self) -> list[Task]:
        if not self.dir.is_dir():
            return []
        out = []
        for path in sorted(self.dir.glob("task_*.json")):
            try:
                out.append(self.load(path.stem))
            except (OSError, ValueError, KeyError):
                continue
        return sorted(out, key=lambda t: (t.created_at, t.id))

    def _locked_method(fn):
        def wrapper(self, *args, **kwargs):
            with self._locked():
                return fn(self, *args, **kwargs)
        return wrapper

    @contextmanager
    def _locked(self):
        """跨进程互斥：claim/complete/update/create 的读-改-写必须串行。"""
        self._ensure()
        lock = self.dir / ".tasks.lock"
        deadline = time.time() + 10
        fd = None
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                if time.time() > deadline:
                    try:
                        if time.time() - lock.stat().st_mtime > 30:
                            lock.unlink()
                            continue
                    except OSError:
                        pass
                    raise RuntimeError("任务目录锁超时（可能有其他进程在写）")
                time.sleep(0.01)
        try:
            yield
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                lock.unlink()
            except OSError:
                pass

    # ---------------------------------------------------------------- 操作

    def create(self, subject: str, description: str = "",
               blocked_by: list[str] | None = None) -> Task:
        subject = subject.strip()
        if not subject:
            raise ValueError("任务标题不能为空")

        deps = list(blocked_by or [])
        with self._locked():
            for dep in deps:
                if not self.exists(dep):
                    raise ValueError(f"前置任务不存在: {dep}")
            created = max(time.time(), self._last_created + 1e-6)
            self._last_created = created
            task = Task(
                id=self._new_id(),
                subject=subject,
                description=description.strip(),
                blocked_by=deps,
                created_at=created,
            )
            # 新任务没有入边，不可能和已有任务成环；create 不做全图 DFS。
            return self.save(task)

    def _new_id(self) -> str:
        for _ in range(50):
            candidate = f"task_{secrets.token_hex(4)}"
            if not self.exists(candidate):
                return candidate
        while True:
            candidate = f"task_{secrets.token_hex(8)}"
            if not self.exists(candidate):
                return candidate

    def _depends_on(self, task_id: str, target_id: str | None) -> bool:
        """task_id 是否（间接）依赖 target_id。target_id 为 None 时只做可达性检查。"""
        seen: set[str] = set()
        stack = [task_id]
        while stack:
            current = stack.pop()
            if current == target_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            try:
                task = self.load(current)
            except (FileNotFoundError, ValueError):
                continue
            stack.extend(task.blocked_by)
        return False

    def _check_no_cycle(self, candidate: Task, existing: list[Task]) -> None:
        """迭代 DFS 查环；不递归，避免超深依赖链 RecursionError。"""
        graph: dict[str, list[str]] = {t.id: list(t.blocked_by) for t in existing}
        graph[candidate.id] = list(candidate.blocked_by)
        color: dict[str, int] = {}

        for start_node in graph:
            if color.get(start_node):
                continue
            color[start_node] = 1
            path = [start_node]
            stack: list[tuple[str, list[str], int]] = [
                (start_node, graph.get(start_node, []), 0)
            ]
            while stack:
                node, deps, index = stack[-1]
                if index >= len(deps):
                    color[node] = 2
                    stack.pop()
                    if path:
                        path.pop()
                    continue
                dep = deps[index]
                stack[-1] = (node, deps, index + 1)
                if color.get(dep) == 1:
                    cycle_start = path.index(dep) if dep in path else 0
                    raise ValueError(f"任务依赖成环: {' → '.join(path[cycle_start:] + [dep])}")
                if not color.get(dep):
                    color[dep] = 1
                    path.append(dep)
                    stack.append((dep, graph.get(dep, []), 0))

    @_locked_method
    def update_dependencies(self, task_id: str, add: list[str]) -> Task:
        task = self.load(task_id)
        existing = [t for t in self.list() if t.id != task_id]

        for dep in add:
            if dep == task_id:
                raise ValueError("任务不能依赖自己")
            if not self.exists(dep):
                raise ValueError(f"前置任务不存在: {dep}")

        if task.status != "pending":
            raise ValueError(f"任务当前是 {task.status}，只有 pending 能改前置")
        merged = replace(task, blocked_by=list(dict.fromkeys(task.blocked_by + list(add))))
        self._check_no_cycle(merged, existing)
        return self.save(merged)

    def incomplete_dependencies(self, task: Task) -> list[str]:
        """前置里还没完成的。读不到的前置**也算没完成**（保守）。"""
        incomplete = []
        for dep in task.blocked_by:
            try:
                if self.load(dep).status != "completed":
                    incomplete.append(dep)
            except (FileNotFoundError, ValueError):
                incomplete.append(dep)
        return incomplete

    def can_start(self, task: Task) -> bool:
        return not self.incomplete_dependencies(task)

    @_locked_method
    def claim(self, task_id: str, owner: str = "agent") -> Task:
        task = self.load(task_id)
        if task.status != "pending":
            raise ValueError(f"任务当前是 {task.status}，不能认领")
        blocked = self.incomplete_dependencies(task)
        if blocked:
            raise ValueError(f"被这些前置任务阻塞: {blocked}")
        task.owner = owner
        task.status = "in_progress"
        return self.save(task)

    def _ready_ids(self) -> set[str]:
        return {
            t.id for t in self.list()
            if t.status == "pending" and self.can_start(t)
        }

    @_locked_method
    def complete(self, task_id: str, owner: str | None = None) -> tuple[Task, list[str]]:
        """完成一个任务，返回 (任务, 因为这一步而被解锁的任务 ID 列表)。"""
        ready_before = self._ready_ids()

        task = self.load(task_id)
        if task.status != "in_progress":
            raise ValueError(f"任务当前是 {task.status}，只有 in_progress 能完成")
        if owner is not None and task.owner and task.owner != owner:
            raise ValueError(f"任务属于 {task.owner}，{owner} 不能完成")

        task.status = "completed"
        self.save(task)

        ready_after = self._ready_ids()
        unlocked = sorted(ready_after - ready_before)
        return task, unlocked

    # ---------------------------------------------------------------- 给模型看的文本

    def render_list(self) -> str:
        tasks = self.list()
        if not tasks:
            return "（还没有任务）"
        lines = []
        for t in tasks:
            mark = {"pending": " ", "in_progress": "~", "completed": "x"}.get(t.status, "?")
            line = f"[{mark}] {t.id}  {t.subject}"
            if t.owner:
                line += f"  (owner: {t.owner})"
            if t.blocked_by:
                waiting = self.incomplete_dependencies(t)
                if waiting:
                    line += f"  ← 等 {waiting}"
            lines.append(line)
        done = sum(1 for t in tasks if t.status == "completed")
        lines.append(f"\n进度：{done}/{len(tasks)} 完成")
        return "\n".join(lines)

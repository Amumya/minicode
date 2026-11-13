"""一次 run 的状态，以及存它的地方。

SessionStore 抽成协议，是因为阶段 F 要换成 Redis。
现在只有内存实现 —— 进程一重启就全没了，这个限制写在这里而不是藏着。
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class RunState:
    id: str
    task: str
    model: str
    workspace: str
    status: str = "queued"          # queued / running / done / error
    events: list[dict] = field(default_factory=list)
    result: dict | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def to_dict(self, include_events: bool = False) -> dict:
        out = {
            "id": self.id,
            "task": self.task,
            "model": self.model,
            "workspace": self.workspace,
            "status": self.status,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "event_count": len(self.events),
        }
        if include_events:
            out["events"] = list(self.events)
        return out


class SessionStore(Protocol):
    def create(self, run: RunState) -> RunState: ...
    def get(self, run_id: str) -> RunState | None: ...
    def all(self) -> list[RunState]: ...


class InMemoryStore:
    def __init__(self, limit: int = 200) -> None:
        self._runs: dict[str, RunState] = {}
        self._limit = limit

    def create(self, run: RunState) -> RunState:
        self._runs[run.id] = run
        # 只留最近 limit 条，防止长时间跑把内存吃干净
        while len(self._runs) > self._limit:
            oldest = next(iter(self._runs))
            self._runs.pop(oldest, None)
        return run

    def get(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def all(self) -> list[RunState]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)


def new_id() -> str:
    return uuid.uuid4().hex[:12]

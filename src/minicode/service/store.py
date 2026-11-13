"""一次 run 的状态，以及存它的地方。

有两个实现：

    InMemoryStore  进程内存，重启就没。开发和测试用。
    RedisStore     放 Redis，进程重启还能查，也能接着跑。

接口故意做成"事件按序号读"的形式（`read_events(start)`），
这样 SSE 断线重连、跨进程补发都不需要改调用方 —— 内存版和 Redis 版行为一致。

一个已经踩到的坑：这台机器上装的是 Redis 5.0，它不支持 RESP3 的 `HELLO` 命令，
而 redis-py 8.x 默认就用 RESP3。所以必须显式 `protocol=2`，否则连不上。
"""

from __future__ import annotations

import json
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
    result: dict | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    event_count: int = 0
    include_files: bool = False

    def to_dict(self, events: list[dict] | None = None) -> dict:
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
            "event_count": self.event_count,
            "include_files": self.include_files,
        }
        if events is not None:
            out["events"] = events
        return out


class SessionStore(Protocol):
    def create(self, run: RunState) -> RunState: ...
    def get(self, run_id: str) -> RunState | None: ...
    def all(self) -> list[RunState]: ...
    def update(self, run: RunState) -> None: ...
    def append_event(self, run_id: str, event: dict) -> int: ...
    def read_events(self, run_id: str, start: int = 0) -> list[dict]: ...
    def save_checkpoint(self, run_id: str, checkpoint: dict) -> None: ...
    def get_checkpoint(self, run_id: str) -> dict | None: ...


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class InMemoryStore:
    def __init__(self, limit: int = 200) -> None:
        self._runs: dict[str, RunState] = {}
        self._events: dict[str, list[dict]] = {}
        self._checkpoints: dict[str, dict] = {}
        self._limit = limit

    def create(self, run: RunState) -> RunState:
        self._runs[run.id] = run
        self._events.setdefault(run.id, [])
        while len(self._runs) > self._limit:
            oldest = next(iter(self._runs))
            self._runs.pop(oldest, None)
            self._events.pop(oldest, None)
            self._checkpoints.pop(oldest, None)
        return run

    def get(self, run_id: str) -> RunState | None:
        return self._runs.get(run_id)

    def all(self) -> list[RunState]:
        return sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)

    def update(self, run: RunState) -> None:
        run.updated_at = time.time()
        self._runs[run.id] = run

    def append_event(self, run_id: str, event: dict) -> int:
        bucket = self._events.setdefault(run_id, [])
        bucket.append(event)
        run = self._runs.get(run_id)
        if run is not None:
            run.event_count = len(bucket)
            run.updated_at = time.time()
        return len(bucket) - 1

    def read_events(self, run_id: str, start: int = 0) -> list[dict]:
        return list(self._events.get(run_id, [])[start:])

    def save_checkpoint(self, run_id: str, checkpoint: dict) -> None:
        self._checkpoints[run_id] = checkpoint

    def get_checkpoint(self, run_id: str) -> dict | None:
        return self._checkpoints.get(run_id)


class RedisStore:
    """Redis 版。key 的约定写在这，别的地方不要再拼 key。"""

    PREFIX = "minicode"

    def __init__(self, url: str = "redis://127.0.0.1:6380/0", ttl_seconds: int = 86400,
                 client=None) -> None:
        if client is not None:
            self._r = client
        else:
            import redis  # 延迟导入：用内存版的人不需要装 redis

            # protocol=2 见模块开头说明：Redis 5 不认 HELLO/RESP3
            self._r = redis.Redis.from_url(url, decode_responses=True, protocol=2)
        self._ttl = ttl_seconds

    # key 都集中在这里生成
    def _k_run(self, run_id: str) -> str:
        return f"{self.PREFIX}:run:{run_id}"

    def _k_events(self, run_id: str) -> str:
        return f"{self.PREFIX}:run:{run_id}:events"

    def _k_ckpt(self, run_id: str) -> str:
        return f"{self.PREFIX}:run:{run_id}:checkpoint"

    def _k_index(self) -> str:
        return f"{self.PREFIX}:runs"

    def _touch(self, *keys: str) -> None:
        for k in keys:
            self._r.expire(k, self._ttl)

    def create(self, run: RunState) -> RunState:
        self._r.hset(self._k_run(run.id), mapping={
            "id": run.id,
            "task": run.task,
            "model": run.model,
            "workspace": run.workspace,
            "status": run.status,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "event_count": 0,
            "include_files": int(run.include_files),
        })
        self._r.zadd(self._k_index(), {run.id: run.created_at})
        self._touch(self._k_run(run.id), self._k_index())
        return run

    def get(self, run_id: str) -> RunState | None:
        data = self._r.hgetall(self._k_run(run_id))
        if not data:
            return None
        return RunState(
            id=data["id"],
            task=data["task"],
            model=data["model"],
            workspace=data["workspace"],
            status=data["status"],
            result=json.loads(data["result"]) if data.get("result") else None,
            error=data.get("error") or None,
            created_at=float(data["created_at"]),
            updated_at=float(data["updated_at"]),
            event_count=int(data.get("event_count", 0)),
            include_files=bool(int(data.get("include_files", 0))),
        )

    def all(self) -> list[RunState]:
        ids = self._r.zrevrange(self._k_index(), 0, 99)
        runs = [self.get(rid) for rid in ids]
        return [r for r in runs if r is not None]

    def update(self, run: RunState) -> None:
        run.updated_at = time.time()
        mapping = {
            "status": run.status,
            "updated_at": run.updated_at,
        }
        if run.result is not None:
            mapping["result"] = json.dumps(run.result, ensure_ascii=False)
        if run.error is not None:
            mapping["error"] = run.error
        self._r.hset(self._k_run(run.id), mapping=mapping)
        self._touch(self._k_run(run.id))

    def append_event(self, run_id: str, event: dict) -> int:
        idx = int(self._r.rpush(self._k_events(run_id),
                               json.dumps(event, ensure_ascii=False)) - 1)
        self._r.hset(self._k_run(run_id), mapping={
            "event_count": idx + 1, "updated_at": time.time()})
        self._touch(self._k_events(run_id), self._k_run(run_id))
        return idx

    def read_events(self, run_id: str, start: int = 0) -> list[dict]:
        raw = self._r.lrange(self._k_events(run_id), start, -1)
        return [json.loads(x) for x in raw]

    def save_checkpoint(self, run_id: str, checkpoint: dict) -> None:
        self._r.set(self._k_ckpt(run_id),
                    json.dumps(checkpoint, ensure_ascii=False))
        self._touch(self._k_ckpt(run_id))

    def get_checkpoint(self, run_id: str) -> dict | None:
        raw = self._r.get(self._k_ckpt(run_id))
        return json.loads(raw) if raw else None

    def clear(self) -> None:
        """测试用：把这个前缀下的 key 清掉。"""
        for pattern in (f"{self.PREFIX}:run:*", self._k_index()):
            for key in self._r.scan_iter(pattern):
                self._r.delete(key)

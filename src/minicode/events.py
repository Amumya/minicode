"""运行事件记录。

为什么单独一个模块：agent 调了什么工具、被拒了几次、花了多少 token，
这些如果不落盘，就只能靠"看日志感觉一下"。落成结构化事件之后，
后面算指标、做对比都从事件里读，不用再解析自由文本。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable


@dataclass
class Event:
    kind: str
    ts: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)


class Recorder:
    """把事件攒在内存里，最后一次性写 JSON。

    on_event 是给服务层用的：事件一产生就能推给订阅者，不用等整个 run 结束。
    不传就退化成原来"只存不发"的行为。
    """

    def __init__(self, on_event: Callable[[Event], None] | None = None) -> None:
        self.events: list[Event] = []
        self._on_event = on_event

    def add(self, kind: str, **data) -> Event:
        ev = Event(kind=kind, data=data)
        self.events.append(ev)
        if self._on_event is not None:
            self._on_event(ev)
        return ev

    def kinds(self) -> list[str]:
        return [e.kind for e in self.events]

    def count(self, kind: str) -> int:
        return sum(1 for e in self.events if e.kind == kind)

    def to_dict(self) -> dict:
        return {"events": [asdict(e) for e in self.events]}

    def dump(self, path: Path, extra: dict | None = None) -> Path:
        payload = self.to_dict()
        if extra:
            payload.update(extra)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

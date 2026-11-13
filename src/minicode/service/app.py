"""HTTP 服务层。

三个设计决定：

1. **run 在后台线程里跑，接口立刻返回 id。**
   一个任务要几十秒，同步等会把 HTTP 请求挂死。

2. **事件用 SSE 推，不用 WebSocket。**
   这里只需要服务端往客户端单向推，SSE 够用，而且 `curl` 就能验证，不用写客户端。
   SSE 还有个好处：断线重连后能从已有事件继续推，因为事件都留在 RunState 里。

3. **默认只监听 127.0.0.1。**
   这个服务没有鉴权，别往公网开。

一个已知限制：内存存储，进程重启就没了。阶段 F 换 Redis 会解决。
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from minicode import __version__
from minicode.config import Settings
from minicode.events import Event, Recorder
from minicode.loop import run as run_loop
from minicode.prompts import default_system
from minicode.service.store import InMemoryStore, RunState, new_id
from minicode.tools import default_registry

ProviderFactory = Callable[[Settings], object]


class RunRequest(BaseModel):
    task: str = Field(min_length=1, description="任务内容")
    model: str | None = None
    workspace: str | None = None
    max_steps: int | None = None
    include_files: bool = False
    save: bool = False


def create_app(
    *,
    provider_factory: ProviderFactory | None = None,
    settings: Settings | None = None,
    store: InMemoryStore | None = None,
    default_workspace: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="minicode", version=__version__)
    st = store or InMemoryStore()
    base_workspace = Path(default_workspace or Path.cwd()).resolve()

    # 每个 run 一个 asyncio.Event，用来通知 SSE 生成器"有新事件了"。
    # 单独放一个 dict，是为了让 store 保持纯数据、不掺和异步。
    changed: dict[str, asyncio.Event] = {}

    def _factory(settings_obj: Settings):
        if provider_factory is not None:
            return provider_factory(settings_obj)
        from minicode.provider import AnthropicProvider

        return AnthropicProvider(settings_obj)

    def _emit(state: RunState, cs: asyncio.Event, ev: Event, loop_ref) -> None:
        state.events.append({"kind": ev.kind, "ts": ev.ts, "data": ev.data})
        state.updated_at = ev.ts
        try:
            loop_ref.call_soon_threadsafe(cs.set)
        except RuntimeError:
            # 事件循环已经关了（比如服务正在退出），丢掉这条就行
            pass

    def _worker(state: RunState, cs: asyncio.Event, loop_ref,
                run_settings: Settings, workspace: Path, include_files: bool) -> None:
        state.status = "running"
        state.updated_at = time.time()
        rec = Recorder(on_event=lambda ev: _emit(state, cs, ev, loop_ref))
        try:
            result = run_loop(
                state.task,
                provider=_factory(run_settings),
                tools=default_registry(include_files=include_files),
                settings=run_settings,
                workspace=workspace,
                recorder=rec,
                system=default_system(),
            )
            state.result = result.to_dict()
            state.status = "done"
        except Exception as exc:  # noqa: BLE001 - 后台线程的异常必须自己接住
            state.error = f"{type(exc).__name__}: {exc}"
            state.status = "error"
        finally:
            state.updated_at = time.time()
            try:
                loop_ref.call_soon_threadsafe(cs.set)
            except RuntimeError:
                pass

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True, "version": __version__, "runs": len(st.all())}

    @app.post("/runs", status_code=202)
    async def create_run(req: RunRequest) -> dict:
        run_settings = settings or Settings.from_env(base_workspace / ".env")
        run_settings = Settings(
            api_key=run_settings.api_key,
            base_url=run_settings.base_url,
            model=req.model or run_settings.model,
            max_steps=req.max_steps or run_settings.max_steps,
        )
        if provider_factory is None and not run_settings.api_key:
            raise HTTPException(400, "没有 API key，先把 .env 配好")

        workspace = Path(req.workspace) if req.workspace else base_workspace
        state = RunState(
            id=new_id(),
            task=req.task,
            model=run_settings.model,
            workspace=str(workspace.resolve()),
        )
        st.create(state)

        loop_ref = asyncio.get_running_loop()
        cs = asyncio.Event()
        changed[state.id] = cs
        threading.Thread(
            target=_worker,
            args=(state, cs, loop_ref, run_settings, workspace, req.include_files),
            daemon=True,
            name=f"run-{state.id}",
        ).start()

        return {"id": state.id, "status": state.status, "model": state.model}

    @app.get("/runs")
    def list_runs() -> dict:
        return {"runs": [r.to_dict() for r in st.all()]}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, events: bool = False) -> dict:
        state = st.get(run_id)
        if state is None:
            raise HTTPException(404, "没有这个 run")
        return state.to_dict(include_events=events)

    @app.get("/runs/{run_id}/events")
    async def stream_events(run_id: str) -> StreamingResponse:
        state = st.get(run_id)
        if state is None:
            raise HTTPException(404, "没有这个 run")
        cs = changed.setdefault(run_id, asyncio.Event())

        async def gen():
            sent = 0
            while True:
                while sent < len(state.events):
                    ev = state.events[sent]
                    sent += 1
                    yield f"event: {ev['kind']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                if state.status in ("done", "error"):
                    tail = {"status": state.status,
                            "result": state.result, "error": state.error}
                    yield f"event: end\ndata: {json.dumps(tail, ensure_ascii=False)}\n\n"
                    return
                cs.clear()
                try:
                    await asyncio.wait_for(cs.wait(), timeout=15)
                except asyncio.TimeoutError:
                    # 心跳，防止中间层把闲置连接掐掉
                    yield ": keepalive\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/")
    def index() -> dict:
        return {
            "name": "minicode",
            "version": __version__,
            "用法": [
                "POST /runs   {\"task\": \"...\"}",
                "GET  /runs/{id}",
                "GET  /runs/{id}/events   (SSE)",
                "GET  /runs",
                "GET  /healthz",
            ],
        }

    return app

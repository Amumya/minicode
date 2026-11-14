"""HTTP 服务层。

几个设计决定：

1. **run 在后台线程里跑，接口立刻返回 id。**
   一个任务要几十秒，同步等会把 HTTP 请求挂死。

2. **事件用 SSE 推，不用 WebSocket。**
   只需要服务端往客户端单向推；SSE 还能用 `curl` 直接验证。
   事件都存在 store 里（内存或 Redis），所以断线重连能从上次的位置接着推。

3. **每轮结束存一次检查点，所以能接着跑。**
   `POST /runs/{id}/resume` 会从最后一个检查点继续，而不是从头开始。
   进程被杀掉也不怕 —— 检查点在 store 里，重启后还在。

4. **默认只监听 127.0.0.1。**
   这个服务没有鉴权，别往公网开。

存储由 `MINICODE_REDIS_URL` 决定：设了就用 Redis，没设就用内存。
内存版重启就丢，这个限制是显式的，不是猜的。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from minicode import __version__
from minicode.compact import CompactConfig, Compactor
from minicode.config import Settings
from minicode.events import Event, Recorder
from minicode.loop import run as run_loop
from minicode.prompts import default_system
from minicode.skills import SkillLoader
from minicode.tasks import TaskStore
from minicode.service.store import (
    InMemoryStore,
    RedisStore,
    RunState,
    SessionStore,
    new_id,
)
from minicode.tools import TodoList, build_registry

ProviderFactory = Callable[[Settings], object]

# 前端构建产物放在包旁边（web/vite.config.ts 里配的输出目录），
# 这样不管是从源码跑还是装成包跑，路径都对得上。
WEB_DIR = Path(__file__).resolve().parent / "web"


class RunRequest(BaseModel):
    task: str = Field(min_length=1, description="任务内容")
    model: str | None = None
    workspace: str | None = None
    max_steps: int | None = None
    include_files: bool = False
    plan: bool = False
    skills: bool = False
    compact: bool = False
    tasks: bool = False


class ResumeRequest(BaseModel):
    max_steps: int | None = None


def default_store() -> SessionStore:
    url = os.environ.get("MINICODE_REDIS_URL", "").strip()
    if url:
        return RedisStore(url)
    return InMemoryStore()


def create_app(
    *,
    provider_factory: ProviderFactory | None = None,
    settings: Settings | None = None,
    store: SessionStore | None = None,
    default_workspace: Path | None = None,
) -> FastAPI:
    app = FastAPI(title="minicode", version=__version__)
    st = store or default_store()
    base_workspace = Path(default_workspace or Path.cwd()).resolve()

    # 每个 run 一个 asyncio.Event，用来叫醒 SSE 生成器。
    # 放 dict 里是为了让 store 保持纯数据、不掺和异步。
    changed: dict[str, asyncio.Event] = {}

    def _factory(settings_obj: Settings):
        if provider_factory is not None:
            return provider_factory(settings_obj)
        from minicode.provider import AnthropicProvider

        return AnthropicProvider(settings_obj)

    def _emit(state: RunState, cs: asyncio.Event, ev: Event, loop_ref) -> None:
        st.append_event(state.id, {"kind": ev.kind, "ts": ev.ts, "data": ev.data})
        state.event_count += 1
        state.updated_at = ev.ts
        try:
            loop_ref.call_soon_threadsafe(cs.set)
        except RuntimeError:
            # 事件循环已关（服务正在退出），丢掉这条即可
            pass

    def _skills_for(state: RunState) -> SkillLoader | None:
        return SkillLoader(Path(state.workspace) / "skills") if state.skills else None

    def _tasks_for(state: RunState):
        return TaskStore(Path(state.workspace) / ".minicode" / "tasks") if state.tasks else None

    def _tools_for(state: RunState):
        return build_registry(
            files=state.include_files,
            todo=TodoList() if state.plan else None,
            skills=_skills_for(state),
            tasks=_tasks_for(state),
        )

    def _worker(state: RunState, cs: asyncio.Event, loop_ref, run_settings: Settings,
                resume: dict | None) -> None:
        state.status = "running"
        st.update(state)
        rec = Recorder(on_event=lambda ev: _emit(state, cs, ev, loop_ref))

        def on_step(payload: dict) -> None:
            try:
                st.save_checkpoint(state.id, payload)
            except Exception:  # noqa: BLE001 - 存检查点失败不能拖垮整个 run
                pass

        todo = TodoList() if state.plan else None
        skills_loader = _skills_for(state)
        compactor = Compactor(Path(state.workspace), CompactConfig()) if state.compact else None

        try:
            result = run_loop(
                state.task,
                provider=_factory(run_settings),
                tools=build_registry(
                    files=state.include_files, todo=todo,
                    skills=skills_loader, tasks=_tasks_for(state)),
                settings=run_settings,
                workspace=Path(state.workspace),
                recorder=rec,
                system=default_system(
                    skills=skills_loader,
                    python_hint=sys.executable,
                    plan_hint=state.plan,
                ),
                resume=resume,
                on_step=on_step,
                compactor=compactor,
            )
            state.result = result.to_dict()
            if todo is not None:
                state.result["todos"] = todo.snapshot()
            state.status = "done"
        except Exception as exc:  # noqa: BLE001 - 后台线程的异常必须自己接住
            state.error = f"{type(exc).__name__}: {exc}"
            state.status = "error"
        finally:
            st.update(state)
            try:
                loop_ref.call_soon_threadsafe(cs.set)
            except RuntimeError:
                pass

    def _start(state: RunState, run_settings: Settings, resume: dict | None) -> None:
        loop_ref = asyncio.get_running_loop()
        cs = asyncio.Event()
        changed[state.id] = cs
        threading.Thread(
            target=_worker,
            args=(state, cs, loop_ref, run_settings, resume),
            daemon=True,
            name=f"run-{state.id}",
        ).start()

    def _resolve_settings(model: str | None, max_steps: int | None) -> Settings:
        base = settings or Settings.from_env(base_workspace / ".env")
        return Settings(
            api_key=base.api_key,
            base_url=base.base_url,
            model=model or base.model,
            max_steps=max_steps or base.max_steps,
        )

    @app.get("/healthz")
    def healthz() -> dict:
        runs = st.all()
        return {
            "ok": True,
            "version": __version__,
            "runs": len(runs),
            "store": type(st).__name__,
        }

    @app.post("/runs", status_code=202)
    async def create_run(req: RunRequest) -> dict:
        run_settings = _resolve_settings(req.model, req.max_steps)
        if provider_factory is None and not run_settings.api_key:
            raise HTTPException(400, "没有 API key，先把 .env 配好")

        workspace = Path(req.workspace) if req.workspace else base_workspace
        state = RunState(
            id=new_id(),
            task=req.task,
            model=run_settings.model,
            workspace=str(workspace.resolve()),
            include_files=req.include_files,
            plan=req.plan,
            skills=req.skills,
            compact=req.compact,
            tasks=req.tasks,
        )
        st.create(state)
        _start(state, run_settings, resume=None)
        return {"id": state.id, "status": state.status, "model": state.model}

    @app.post("/runs/{run_id}/resume", status_code=202)
    async def resume_run(run_id: str, req: ResumeRequest | None = None) -> dict:
        state = st.get(run_id)
        if state is None:
            raise HTTPException(404, "没有这个 run")
        if state.status == "running":
            raise HTTPException(409, "这个 run 还在跑，不用恢复")
        checkpoint = st.get_checkpoint(run_id)
        if not checkpoint:
            raise HTTPException(409, "没有检查点，只能从头跑")

        run_settings = _resolve_settings(
            state.model, (req.max_steps if req else None))
        state.status = "queued"
        state.result = None
        state.error = None
        st.update(state)
        _start(state, run_settings, resume=checkpoint)
        return {
            "id": state.id,
            "status": state.status,
            "resumed_from_step": checkpoint.get("step"),
        }

    @app.get("/runs")
    def list_runs() -> dict:
        return {"runs": [r.to_dict() for r in st.all()]}

    @app.get("/runs/{run_id}")
    def get_run(run_id: str, events: bool = False) -> dict:
        state = st.get(run_id)
        if state is None:
            raise HTTPException(404, "没有这个 run")
        return state.to_dict(events=st.read_events(run_id) if events else None)

    @app.get("/runs/{run_id}/tasks")
    def get_run_tasks(run_id: str) -> dict:
        """返回这次 run 的工作区里的任务图。

        为什么不从事件里重建：create_task 的 ID 是在工具**结果**里才出现的，
        按事件流反推容易错（模型可能没把 ID 抄对）。直接读任务目录最准。
        """
        state = st.get(run_id)
        if state is None:
            raise HTTPException(404, "没有这个 run")
        if not state.tasks:
            return {"enabled": False, "tasks": [], "progress": {"done": 0, "total": 0}}
        store = TaskStore(Path(state.workspace) / ".minicode" / "tasks")
        tasks = store.list()
        return {
            "enabled": True,
            "tasks": [t.to_public() for t in tasks],
            "progress": {
                "done": sum(1 for t in tasks if t.status == "completed"),
                "total": len(tasks),
            },
        }

    @app.get("/runs/{run_id}/events")
    async def stream_events(run_id: str) -> StreamingResponse:
        state = st.get(run_id)
        if state is None:
            raise HTTPException(404, "没有这个 run")
        cs = changed.setdefault(run_id, asyncio.Event())

        async def gen():
            sent = 0
            while True:
                for ev in st.read_events(run_id, sent):
                    sent += 1
                    yield f"event: {ev['kind']}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"
                fresh = st.get(run_id)
                if fresh is not None and fresh.status in ("done", "error"):
                    # 收尾事件也必须和别的事件同一个形状（kind / ts / data）。
                    # 之前这里直接发 {status, result, error}，少了外面那层，
                    # 前端就把它当普通事件渲染，结果画出一个空 JSON，
                    # 而且 showAnswer(ev.data) 拿到 undefined 直接抛 TypeError。
                    tail = {
                        "kind": "end",
                        "ts": time.time(),
                        "data": {
                            "status": fresh.status,
                            "result": fresh.result,
                            "error": fresh.error,
                        },
                    }
                    yield f"event: end\ndata: {json.dumps(tail, ensure_ascii=False)}\n\n"
                    return
                cs.clear()
                try:
                    await asyncio.wait_for(cs.wait(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api")
    def index() -> dict:
        return {
            "name": "minicode",
            "version": __version__,
            "store": type(st).__name__,
            "用法": [
                "POST /runs   {\"task\": \"...\"}",
                "POST /runs/{id}/resume",
                "GET  /runs/{id}",
                "GET  /runs/{id}/events   (SSE)",
                "GET  /runs",
                "GET  /healthz",
            ],
        }

    # 界面挂在根路径。必须在所有接口注册完之后再挂，
    # 否则这个覆盖 "/" 的 mount 会把后面的接口挡掉。
    if WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
    else:
        @app.get("/")
        def index_no_ui() -> dict:
            return {
                "name": "minicode",
                "version": __version__,
                "注意": "没找到前端产物（web/），先跑 `npm --prefix web run build`",
                "接口": "/api",
            }

    return app
